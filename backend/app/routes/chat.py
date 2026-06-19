"""Chat API routes."""

from fastapi import APIRouter, HTTPException, Request

from ..leads import build_lead_from_contact
from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    MessageRole,
    PolicyAction,
    SessionPublicResponse,
    SessionStatus,
    QuickAction,
)


router = APIRouter(prefix="/api/chat", tags=["chat"])


def build_quick_actions(policy_action: PolicyAction, status: SessionStatus, lead_created: bool) -> list[QuickAction]:
    actions: list[QuickAction] = []

    if status == SessionStatus.AI_ACTIVE:
        actions.extend(
            [
                QuickAction(type="reply", label="Позвать оператора", value="Позовите оператора"),
                QuickAction(type="reply", label="Оставить телефон", value="Хочу оставить телефон"),
            ]
        )

    if status == SessionStatus.WAITING_OPERATOR:
        actions.extend(
            [
                QuickAction(type="reply", label="Дополнить запрос", value="Хочу добавить детали"),
                QuickAction(type="reply", label="Оставить телефон", value="Хочу оставить телефон"),
            ]
        )

    if policy_action in {PolicyAction.CLARIFY, PolicyAction.REJECT}:
        actions.append(QuickAction(type="reply", label="Список услуг", value="Какие услуги есть?"))

    if lead_created:
        actions.append(QuickAction(type="phone", label="Позвонить", value="+74950000000"))

    actions.append(QuickAction(type="open_url", label="Открыть сайт", value="https://www.medcenterrosh.ru/"))
    return actions[:4]


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
        answer = "Ожидаем подключения специалиста. Ваше сообщение сохранено в истории диалога."
        await session_store.append_message(session.session_id, MessageRole.SYSTEM, answer)
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.REJECT,
            answer=answer,
            lead_created=False,
            quick_actions=build_quick_actions(PolicyAction.REJECT, session.status, False),
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
        answer = str(policy_result.safe_context.get("message_to_user") or "")
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
        quick_actions=build_quick_actions(policy_result.action, session.status, lead_created),
    )
