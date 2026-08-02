"""точка входа fastapi."""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .analytics import AnalyticsService
from .config import get_settings
from .delivery import DeliveryService
from .knowledge import KnowledgeBaseResolver
from .leads import LeadService
from .llm import build_llm_client, get_system_prompt
from .policy import analyze_message
from .rate_limit import RateLimiter
from .services.rag_search import rag_corpus_status
from .routes import analytics, chat, debug, delivery, leads, operator, widget, ws
from .sessions import SessionStore
from .telegram_bridge import TelegramBridgeService
from .ws_manager import ConnectionManager


logger = logging.getLogger(__name__)


async def _run_session_eviction_loop(
    session_store: SessionStore,
    *,
    ttl_seconds: int,
    interval_seconds: int,
    snapshot_file: Path | None,
) -> None:
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await session_store.evict_stale(ttl_seconds)
            if snapshot_file is not None:
                await session_store.snapshot_to(snapshot_file, ttl_seconds=ttl_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("session eviction loop error=%s", type(error).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    app.state.settings = settings
    app.state.knowledge_base_resolver = KnowledgeBaseResolver(
        data_dir=settings.data_dir,
        clients_data_dir=settings.clients_data_dir,
        defaults_data_dir=settings.defaults_data_dir,
        default_company_id=settings.default_company_id,
    )
    app.state.knowledge_base_resolver.build_domain_index()
    app.state.knowledge_base = app.state.knowledge_base_resolver.get(settings.default_company_id)
    app.state.rag_corpus_status = rag_corpus_status()
    if app.state.rag_corpus_status["ok"]:
        logger.info(
            "rag corpus loaded path=%s chunks=%d",
            app.state.rag_corpus_status["path"],
            app.state.rag_corpus_status["chunk_count"],
        )
    else:
        # Не падаем — бот всё ещё отвечает по услугам/ценам без статей, это деградация,
        # не полная неработоспособность. Но раньше это узнавали только от клиента, теперь
        # видно в логе при старте и в /health.
        logger.warning(
            "rag corpus MISSING or EMPTY path=%s error=%s — faq answers will degrade to "
            "generic clarify, article guidance disabled",
            app.state.rag_corpus_status["path"],
            app.state.rag_corpus_status["error"],
        )
    app.state.session_store = SessionStore()
    snapshot_file = Path(settings.session_snapshot_file) if settings.session_snapshot_file else None
    if snapshot_file is not None:
        await app.state.session_store.restore_from(snapshot_file)
    app.state.delivery_service = DeliveryService(
        outbox_file=settings.delivery_outbox_file,
        knowledge_base_resolver=app.state.knowledge_base_resolver,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )
    app.state.lead_service = LeadService(leads_file=settings.leads_file, delivery_service=app.state.delivery_service)
    app.state.analytics_service = AnalyticsService(
        analytics_file=settings.analytics_file,
        leads_file=settings.leads_file,
    )
    app.state.llm_client = build_llm_client(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )
    app.state.system_prompt = get_system_prompt()
    app.state.policy_analyzer = analyze_message
    app.state.ws_manager = ConnectionManager(app.state.session_store)
    app.state.chat_rate_limiter = RateLimiter(limit=settings.chat_rate_limit_per_minute)
    app.state.telegram_bridge_service = TelegramBridgeService(
        bot_token=settings.telegram_bot_token,
        group_chat_id=settings.telegram_operators_group_id,
        session_store=app.state.session_store,
        ws_manager=app.state.ws_manager,
    )

    retry_task = None
    if settings.delivery_retry_enabled:
        retry_task = asyncio.create_task(
            app.state.delivery_service.run_retry_loop(settings.delivery_retry_interval_seconds)
        )
    eviction_task = None
    if settings.session_eviction_enabled:
        eviction_task = asyncio.create_task(
            _run_session_eviction_loop(
                app.state.session_store,
                ttl_seconds=settings.session_ttl_seconds,
                interval_seconds=settings.session_eviction_interval_seconds,
                snapshot_file=snapshot_file,
            )
        )
    telegram_bridge_task = None
    if settings.telegram_bridge_enabled:
        telegram_bridge_task = asyncio.create_task(app.state.telegram_bridge_service.run_polling_loop())

    try:
        yield
    finally:
        if telegram_bridge_task is not None:
            telegram_bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telegram_bridge_task
        if eviction_task is not None:
            eviction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await eviction_task
        if retry_task is not None:
            retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry_task
        if snapshot_file is not None:
            await app.state.session_store.snapshot_to(snapshot_file, ttl_seconds=settings.session_ttl_seconds)


app = FastAPI(title="AI Chat Widget MVP", lifespan=lifespan)
settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(leads.router)
app.include_router(analytics.router)
app.include_router(debug.router)
app.include_router(delivery.router)
app.include_router(operator.router)
app.include_router(widget.router)
app.include_router(ws.router)

app.mount("/static", StaticFiles(directory=str(settings.widget_path.parent)), name="static")


@app.get("/health")
async def healthcheck() -> dict[str, object]:
    corpus_status = getattr(app.state, "rag_corpus_status", None) or rag_corpus_status()
    return {
        "status": "ok",
        "current_provider": settings.llm_provider,
        "current_model": settings.llm_model,
        "rag_corpus": corpus_status,
    }


@app.get("/demo/demo.html")
async def demo_page() -> FileResponse:
    return FileResponse(Path(settings.demo_dir) / "demo.html")


@app.get("/demo/external-site.html")
async def external_demo_page() -> FileResponse:
    return FileResponse(Path(settings.demo_dir) / "external-site.html")
