"""Chat API routes."""

from fastapi import APIRouter, HTTPException, Request

from ..leads import build_lead_from_contact
from ..llm import MockLLMClient
from ..policy import classify_and_extract
from ..models import (
    ChatMessageRequest,
    ChatMessageResponse,
    Message,
    MessageRole,
    PolicyAction,
    QuickAction,
    SessionPublicResponse,
    SessionStatus,
)


router = APIRouter(prefix="/api/chat", tags=["chat"])
fallback_llm_client = MockLLMClient()


def format_quick_actions(labels: list[object], request: Request) -> list[QuickAction]:
    company = request.app.state.knowledge_base.company
    values_by_label = {
        "Позвать оператора": ("message", "Хочу поговорить с оператором"),
        "Посмотреть услуги": ("message", "Покажи список услуг"),
        "Оставить телефон": ("message", "Хочу оставить телефон"),
        "Уточнить цену": ("message", "Хочу уточнить цену"),
        "Написать в Telegram": ("link", company.telegram_url or ""),
        "Открыть сайт": ("link", company.website_url or ""),
    }
    normalized_values = {label.casefold(): value for label, value in values_by_label.items()}

    actions: list[QuickAction] = []
    for item in labels:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            action_type = str(item.get("type") or "message").strip()
            value = str(item.get("value") or label).strip()
            if label and value:
                actions.append(QuickAction(label=label, type=action_type, value=value))
            continue

        label = str(item)
        action_type, value = normalized_values.get(label.casefold(), ("message", label))
        if action_type == "link" and not value:
            continue
        actions.append(QuickAction(label=label, type=action_type, value=value))
    return actions


def service_classifier_payload(request: Request) -> list[dict[str, object]]:
    return [
        {
            "id": service.id,
            "name": service.name,
            "synonyms": service.synonyms,
        }
        for service in request.app.state.knowledge_base.services
    ]


async def resolve_classification(message: str, request: Request) -> dict[str, object]:
    known_services = service_classifier_payload(request)
    local_result = classify_and_extract(message, known_services)
    try:
        model_result = await request.app.state.llm_client.classify_and_extract(message, known_services)
    except Exception:
        return local_result

    model_intent = str(model_result.get("intent") or "")
    local_intent = str(local_result.get("intent") or "")
    model_service_id = model_result.get("service_id")
    local_service_id = local_result.get("service_id")
    model_confidence = float(model_result.get("confidence") or 0.0)
    local_confidence = float(local_result.get("confidence") or 0.0)

    if model_intent == "unknown_service":
        return model_result
    if local_service_id and not model_service_id:
        return local_result
    if (
        local_intent
        in {
            "list_services",
            "price_question",
            "medical_advice",
            "operator_request",
            "off_topic",
            "cosmetic_concern",
        }
        and model_intent in {"small_talk", "service_mention"}
        and not model_service_id
    ):
        return local_result
    if model_confidence < 0.5 and local_confidence > model_confidence:
        return local_result
    return model_result


async def safe_small_talk(request: Request, company_name: str, message: str) -> str:
    try:
        return await request.app.state.llm_client.small_talk(company_name, message)
    except Exception:
        return await fallback_llm_client.small_talk(company_name, message)


async def safe_complete(
    request: Request,
    context: dict[str, object],
    message: str,
    history: list[Message],
) -> str:
    try:
        return await request.app.state.llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )
    except Exception:
        return await fallback_llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )


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

    classification = await resolve_classification(payload.message, request)
    policy_result = request.app.state.policy_analyzer(
        payload.message,
        session,
        knowledge_base,
        classification,
    )

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
    elif policy_result.action == PolicyAction.SMALL_TALK:
        answer = await safe_small_talk(request, knowledge_base.company.company_name, payload.message)
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
        answer = str(
            policy_result.safe_context.get("message_to_user")
            or policy_result.safe_context.get("city_note")
            or ""
        )
    elif policy_result.action == PolicyAction.REJECT:
        answer = str(policy_result.safe_context.get("message_to_user") or "Запрос отклонён.")
    else:
        answer = await safe_complete(
            request,
            policy_result.safe_context,
            payload.message,
            session.messages[-8:],
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
