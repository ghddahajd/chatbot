"""api-роуты чата."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    FaqAnswerRequest,
    MessageRole,
    PolicyAction,
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
    if session.status in (SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE):
        # Клиент сам сбросил диалог кнопкой в виджете (в т.ч. посреди живого разговора с
        # оператором) — закрываем сессию и уведомляем Telegram-тему, иначе оператор
        # продолжал бы печатать в тему, не зная, что собеседник уже ушёл.
        system_text = (
            "Пользователь завершил диалог с оператором и начал новый."
            if session.status == SessionStatus.HUMAN_ACTIVE
            else "Пользователь завершил ожидание специалиста и начал новый диалог."
        )
        await session_store.set_status(session_id, SessionStatus.CLOSED)
        await session_store.append_message(session_id, MessageRole.SYSTEM, system_text)
        telegram_bridge = getattr(request.app.state, "telegram_bridge_service", None)
        if telegram_bridge is not None:
            await telegram_bridge.notify_client_left(session_id)
    session = await session_store.get(session_id)
    return {"status": session.status.value}


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse | JSONResponse:
    settings = request.app.state.settings
    if settings.chat_rate_limit_enabled:
        limiter = request.app.state.chat_rate_limiter
        if not limiter.allow(client_ip(request, trusted_proxy_count=settings.trusted_proxy_count)):
            raise HTTPException(status_code=429, detail="Too many chat messages. Please wait.")
    return await ChatService(request).handle_message(
        company_id=payload.company_id,
        session_id=payload.session_id,
        message=payload.message,
    )


@router.post("/faq-answer", response_model=ChatMessageResponse)
async def faq_answer(payload: FaqAnswerRequest, request: Request) -> ChatMessageResponse | JSONResponse:
    """Готовый вопрос-ответ со второго экрана "Частые вопросы" виджета (2026-08-28) —
    ответ приходит из faq_quick_answers.json по faq_id, без похода в LLM/классификацию:
    клиент не может подменить текст, присылая произвольный вопрос — только id уже
    одобренного пункта. Вопрос+ответ всё равно кладём в историю сессии (чтобы оператор,
    если возьмёт диалог позже, видел это в переписке) и трекаем как обычный message_answered
    (чтобы аналитика "по часам"/логи видели эти обращения тоже)."""

    settings = request.app.state.settings
    if settings.chat_rate_limit_enabled:
        limiter = request.app.state.chat_rate_limiter
        if not limiter.allow(client_ip(request, trusted_proxy_count=settings.trusted_proxy_count)):
            raise HTTPException(status_code=429, detail="Too many chat messages. Please wait.")

    resolver = request.app.state.knowledge_base_resolver
    try:
        knowledge_base = resolver.get(payload.company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    faq_item = knowledge_base.find_quick_faq_by_id(payload.faq_id)
    if faq_item is None:
        raise HTTPException(status_code=404, detail="Unknown FAQ item")

    session_store = request.app.state.session_store
    session = await session_store.get_or_create(payload.session_id, payload.company_id)

    await session_store.append_message(session.session_id, MessageRole.USER, faq_item.question)
    await session_store.append_message(session.session_id, MessageRole.ASSISTANT, faq_item.answer)

    analytics_service = getattr(request.app.state, "analytics_service", None)
    if analytics_service is not None:
        try:
            await analytics_service.track_answer(
                company_id=payload.company_id,
                session_id=session.session_id,
                message=faq_item.question,
                answer=faq_item.answer,
                action=PolicyAction.ANSWER.value,
                policy_reason="quick_faq",
            )
        except Exception:
            pass  # аналитика не должна ронять сам ответ клиенту

    session = await session_store.get(session.session_id)
    return ChatMessageResponse(
        session_id=session.session_id,
        status=session.status,
        action=PolicyAction.ANSWER,
        answer=faq_item.answer,
        lead_created=False,
        quick_actions=[],
    )
