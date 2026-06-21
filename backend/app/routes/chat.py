"""api-роуты чата."""

from fastapi import APIRouter, HTTPException, Request

from ..leads import build_lead_from_contact
from ..policy import classify_and_extract
from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    MessageRole,
    PolicyAction,
    PolicyReason,
    QuickAction,
    SessionPublicResponse,
    SessionStatus,
)
from .chat_utils import (
    HAS_LETTER_OR_DIGIT,
    MAX_MESSAGE_LENGTH,
    MAX_SESSION_MESSAGES,
    RATE_LIMIT_ANSWER,
    classify_consultation_medical_risk,
    contextual_affirmative_response,
    format_quick_actions,
    maybe_contextual_classification,
    resolve_classification,
    safe_complete,
    safe_medical_handoff,
    safe_small_talk,
    service_classifier_payload,
    should_use_consultation_llm,
)


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
async def send_message(payload: ChatMessageRequest, request: Request) -> ChatMessageResponse:
    session_store = request.app.state.session_store
    knowledge_base = request.app.state.knowledge_base
    lead_service = request.app.state.lead_service

    session = await session_store.get_or_create(payload.session_id, payload.company_id)
    raw_message = payload.message or ""
    stripped_message = raw_message.strip()

    if not stripped_message:
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.CLARIFY,
            answer="Похоже, сообщение пустое. Напишите вопрос, и я подскажу.",
            lead_created=False,
            quick_actions=[],
        )

    if not HAS_LETTER_OR_DIGIT.search(stripped_message):
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.CLARIFY,
            answer="Не совсем понял вопрос. Можете переформулировать словами?",
            lead_created=False,
            quick_actions=[],
        )

    if session.message_count >= MAX_SESSION_MESSAGES:
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.CLARIFY,
            answer=RATE_LIMIT_ANSWER,
            lead_created=False,
            quick_actions=[
                QuickAction(label="Начать новый диалог", type="message", value="Начать новый диалог")
            ],
        )

    message = stripped_message[:MAX_MESSAGE_LENGTH]
    await session_store.append_message(session.session_id, MessageRole.USER, message)

    if session.status == SessionStatus.WAITING_OPERATOR:
        local_classification = classify_and_extract(
            message,
            service_classifier_payload(request),
            knowledge_base.company.city,
        )
        waiting_policy_result = request.app.state.policy_analyzer(
            message,
            session,
            knowledge_base,
            local_classification,
        )
        contact = waiting_policy_result.safe_context.get("contact")
        if waiting_policy_result.action == PolicyAction.ASK_CONTACT and contact:
            lead = build_lead_from_contact(
                session_id=session.session_id,
                contact=contact,
                summary=message,
                service_id=waiting_policy_result.service_id,
            )
            await lead_service.save(lead)
            await session_store.set_lead_requested(session.session_id, True)
            answer = (
                "Спасибо. Передали ваши контакты специалисту. "
                "С вами свяжутся для уточнения деталей."
            )
            await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
            session = await session_store.get(session.session_id)
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=waiting_policy_result.action,
                answer=answer,
                lead_created=True,
                quick_actions=[],
            )

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

    policy_result = contextual_affirmative_response(message, session)
    if policy_result is None:
        classification = maybe_contextual_classification(message, session)
        if classification is None:
            classification = await resolve_classification(message, request)
        policy_result = request.app.state.policy_analyzer(
            message,
            session,
            knowledge_base,
            classification,
        )

    lead_created = False
    answer = ""
    response_action = policy_result.action
    response_quick_actions = policy_result.quick_actions

    if policy_result.action == PolicyAction.ASK_CONTACT:
        contact = policy_result.safe_context.get("contact")
        if contact:
            is_booking_request = bool(policy_result.safe_context.get("booking_request"))
            lead = build_lead_from_contact(
                session_id=session.session_id,
                contact=contact,
                summary=("Заявка на запись: " + message) if is_booking_request else message,
                service_id=policy_result.service_id,
            )
            await lead_service.save(lead)
            lead_created = True
            await session_store.set_lead_requested(session.session_id, True)
            if is_booking_request:
                answer = (
                    "Спасибо. Заявку на запись передали. "
                    "С вами свяжутся, чтобы подтвердить время и детали."
                )
            else:
                answer = (
                    "Спасибо. Передали ваши контакты специалисту. "
                    "С вами свяжутся для уточнения деталей."
                )
            if not is_booking_request and (session.operator_requested or policy_result.service_id is None):
                await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        else:
            answer = str(policy_result.safe_context.get("message_to_user") or "")
            await session_store.set_operator_requested(session.session_id, True)
            await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
    elif policy_result.action == PolicyAction.SMALL_TALK:
        answer = await safe_small_talk(request, knowledge_base.company.company_name, message)
    elif policy_result.action == PolicyAction.OFF_TOPIC:
        answer = str(policy_result.safe_context.get("message_to_user") or "")
    elif policy_result.action == PolicyAction.TRANSFER_OPERATOR:
        await session_store.set_operator_requested(session.session_id, True)
        await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        answer = str(
            policy_result.safe_context.get("handoff_message")
            or policy_result.safe_context.get("message_to_user")
            or "Передаю диалог специалисту. Оператор увидит историю переписки."
        )
    elif policy_result.action == PolicyAction.CLARIFY:
        direct_clarify_reasons = {
            PolicyReason.OPERATOR_REQUESTED,
            PolicyReason.LOCATION_MISMATCH,
            PolicyReason.UNSUPPORTED_CITY,
        }
        if policy_result.reason in direct_clarify_reasons or policy_result.safe_context.get("force_direct_answer"):
            answer = str(
                policy_result.safe_context.get("message_to_user")
                or policy_result.safe_context.get("city_note")
                or ""
            )
        else:
            answer = await safe_complete(
                request,
                policy_result.safe_context,
                message,
                session.messages[-8:],
            )
    elif policy_result.action == PolicyAction.REJECT:
        answer = str(policy_result.safe_context.get("message_to_user") or "Запрос отклонён.")
    elif should_use_consultation_llm(policy_result.safe_context):
        medical_risk, _request_id = await classify_consultation_medical_risk(
            request,
            message,
            policy_result.safe_context,
        )
        if medical_risk == "MEDICAL":
            await session_store.set_operator_requested(session.session_id, True)
            await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
            answer = await safe_medical_handoff(request, message)
            response_action = PolicyAction.TRANSFER_OPERATOR
            response_quick_actions = ["Оставить телефон"]
        else:
            answer = await safe_complete(
                request,
                policy_result.safe_context,
                message,
                session.messages[-8:],
            )
    else:
        answer = await safe_complete(
            request,
            policy_result.safe_context,
            message,
            session.messages[-8:],
        )

    await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
    session = await session_store.get(session.session_id)

    return ChatMessageResponse(
        session_id=session.session_id,
        status=session.status,
        action=response_action,
        answer=answer,
        lead_created=lead_created,
        quick_actions=format_quick_actions(response_quick_actions, request),
    )
