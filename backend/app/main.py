"""точка входа fastapi."""

import asyncio
import contextlib
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
from .routes import analytics, chat, debug, delivery, leads, operator, widget, ws
from .sessions import SessionStore
from .ws_manager import ConnectionManager


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
    app.state.session_store = SessionStore()
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

    retry_task = None
    if settings.delivery_retry_enabled:
        retry_task = asyncio.create_task(
            app.state.delivery_service.run_retry_loop(settings.delivery_retry_interval_seconds)
        )

    try:
        yield
    finally:
        if retry_task is not None:
            retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry_task


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
async def healthcheck() -> dict[str, str]:
    return {
        "status": "ok",
        "current_provider": settings.llm_provider,
        "current_model": settings.llm_model,
    }


@app.get("/demo/demo.html")
async def demo_page() -> FileResponse:
    return FileResponse(Path(settings.demo_dir) / "demo.html")


@app.get("/demo/external-site.html")
async def external_demo_page() -> FileResponse:
    return FileResponse(Path(settings.demo_dir) / "external-site.html")
