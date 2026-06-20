"""FastAPI entrypoint."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .knowledge import KnowledgeBase
from .leads import LeadService
from .llm import build_llm_client, get_system_prompt
from .policy import analyze_message
from .routes import chat, leads, operator, ws
from .sessions import SessionStore
from .ws_manager import ConnectionManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    app.state.settings = settings
    app.state.knowledge_base = KnowledgeBase.load(settings.data_dir)
    app.state.session_store = SessionStore()
    app.state.lead_service = LeadService(
        leads_file=settings.leads_file,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
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
    yield


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
app.include_router(operator.router)
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
