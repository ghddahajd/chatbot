"""api-роуты чата."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    MessageRole,
    SessionPublicResponse,
    SessionStatus,
)
from ..rate_limit import client_ip
from ..services.chat_service import ChatService


router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/session/{session_id}", response_model=SessionPublicResponse)
async def get_session(session_id: str, request: Request) -> SessionPublicResponse:
    session = await request.app.state.session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionPublicResponse(**session.model_dump())


@router.post("/session/{session_id}/cancel")
async def cancel_session(session_id: str, request: Request) -> dict[str, str]:
    session_store = request.app.state.session_store
    session = await session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == SessionStatus.WAITING_OPERATOR:
        await session_store.set_status(session_id, SessionStatus.CLOSED)
        await session_store.append_message(
            session_id,
            MessageRole.SYSTEM,
            "Пользователь завершил ожидание специалиста и начал новый диалог.",
        )
    session = await session_store.get(session_id)
    return {"status": session.status.value}


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse | JSONResponse:
    settings = request.app.state.settings
    if settings.chat_rate_limit_enabled:
        limiter = request.app.state.chat_rate_limiter
        if not limiter.allow(client_ip(request)):
            raise HTTPException(status_code=429, detail="Too many chat messages. Please wait.")
    return await ChatService(request).handle_message(
        company_id=payload.company_id,
        session_id=payload.session_id,
        message=payload.message,
    )
