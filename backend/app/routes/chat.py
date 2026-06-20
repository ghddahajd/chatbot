"""Chat API routes."""

from fastapi import APIRouter, HTTPException, Request

from ..leads import build_lead_from_contact
from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    MessageRole,
    PolicyAction,
    QuickAction,
    SessionPublicResponse,
    SessionStatus,
)


router = APIRouter(prefix="/api/chat", tags=["chat"])


def format_quick_actions(labels: list[str], request: Request) -> list[QuickAction]:
    company = request.app.state.knowledge_base.company
    values = {
        "Позвать оператора": ("message", "Хочу поговорить с оператором"),
        "Посмотреть услуги": ("message", "Покажи список услуг"),
        "Оставить телефон": ("message", "Хочу оставить телефон"),
        "Уточнить цену": ("message", "Хочу уточнить цену"),
        "Написать в Telegram": ("link", company.telegram_url or ""),
        "Открыть сайт": ("link", company.website_url or ""),
    }

    actions: list[QuickAction] = []
    for label in labels:
        action_type, value = values.get(label, ("message", label))
        if action_type == "link" and not value:
            continue
        actions.append(QuickAction(label=label, type=action_type, value=value))
    return actions


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
async def send_message(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse:
    session_store = request.app.state.session_store
    knowledge_base = request.app.state.knowledge_base
    llm_client = request.app.state.llm_client
    lead_service = request.app.state.lead_service

    session = await session_store.get_or_create(payload.session_id, payload.company_id)
    await session_store.append_message(session.session_id, MessageRole.USER, payload.message)

    if session.status == SessionStatus.WAITING_OPERATOR:
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.REJECT,
            answer="",
            lead_created=False,
            quick_actions=[],
        )

    if session.status == SessionStatus.HUMAN_ACTIVE:
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.REJECT,
            answer="Чат передан специалисту. Пожалуйста, дождитесь ответа оператора.",
            lead_created=False,
            quick_actions=[],
        )

    policy_result = request.app.state.policy_analyzer(payload.message, session, knowledge_base)

    lead_created = False
    answer = ""

    if policy_result.action == PolicyAction.ASK_CONTACT:
        contact = policy_result.safe_context.get("contact")
        if contact:
            lead = build_lead_from_contact(
                session_id=session.session_id,
                contact=contact,
                summary=payload.message,
                service_id=policy_result.service_id,
            )
            await lead_service.save(lead)
            lead_created = True
            await session_store.set_lead_requested(session.session_id, True)
            answer = (
                "Спасибо. Передали ваши контакты специалисту. "
                "С вами свяжутся для уточнения деталей."
            )
            if session.operator_requested or policy_result.service_id is None:
                await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        else:
            answer = str(policy_result.safe_context.get("message_to_user") or "")
            await session_store.set_operator_requested(session.session_id, True)
            await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
    elif policy_result.action == PolicyAction.TRANSFER_OPERATOR:
        await session_store.set_operator_requested(session.session_id, True)
        await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        answer = str(
            policy_result.safe_context.get("handoff_message")
            or policy_result.safe_context.get("message_to_user")
            or "Передаю диалог специалисту. Оператор увидит историю переписки."
        )
    elif policy_result.action == PolicyAction.CLARIFY:
        answer = str(
            policy_result.safe_context.get("message_to_user")
            or policy_result.safe_context.get("city_note")
            or ""
        )
    elif policy_result.action == PolicyAction.REJECT:
        answer = str(policy_result.safe_context.get("message_to_user") or "Запрос отклонён.")
    else:
        answer = await llm_client.complete(
            request.app.state.system_prompt,
            policy_result.safe_context,
            payload.message,
        )

    await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
    session = await session_store.get(session.session_id)

    return ChatMessageResponse(
        session_id=session.session_id,
        status=session.status,
        action=policy_result.action,
        answer=answer,
        lead_created=lead_created,
        quick_actions=format_quick_actions(policy_result.quick_actions, request),
    )
