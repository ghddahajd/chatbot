"""сервис обработки сообщений чата."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from ..leads import build_lead_from_contact
from ..models import (
    ChatMessageResponse,
    MessageRole,
    PolicyAction,
    PolicyReason,
    QuickAction,
    SessionStatus,
)
from ..policy import classify_and_extract
from ..routes.chat_utils import (
    CONSULTATION_RISK_RESTRICTED,
    HAS_LETTER_OR_DIGIT,
    MAX_MESSAGE_LENGTH,
    MAX_SESSION_MESSAGES,
    RATE_LIMIT_ANSWER,
    classify_consultation_risk,
    contextual_affirmative_response,
    format_quick_actions,
    maybe_contextual_classification,
    resolve_classification,
    safe_complete,
    safe_restricted_handoff,
    safe_small_talk,
    service_classifier_payload,
    should_use_consultation_llm,
)


logger = logging.getLogger(__name__)


class ChatService:
    """оркестрирует обработку одного пользовательского сообщения."""

    def __init__(self, request: Request) -> None:
        self.request = request

    def _phrase(self, key: str, fallback: str) -> str:
        phrasebook = getattr(self, "_phrasebook", {})
        value = phrasebook.get(key) if isinstance(phrasebook, dict) else None
        return str(value).strip() if value else fallback

    def _operator_url(self) -> str:
        try:
            return str(self.request.url_for("operator_page"))
        except Exception:
            return "/operator"

    async def _enqueue_operator_requested(self, *, company_id: str, session_id: str, message: str) -> None:
        delivery_service = getattr(self.request.app.state, "delivery_service", None)
        if delivery_service is None:
            return

        try:
            await delivery_service.enqueue_event(
                event_type="operator_requested",
                company_id=company_id,
                session_id=session_id,
                payload={
                    "last_message": message,
                    "operator_url": self._operator_url(),
                },
            )
        except Exception as error:
            logger.warning(
                "operator delivery enqueue failed company_id=%s session_id=%s error=%s",
                company_id,
                session_id,
                type(error).__name__,
            )

    async def handle_message(
        self,
        *,
        company_id: str,
        session_id: str | None,
        message: str,
    ) -> ChatMessageResponse | JSONResponse:
        request = self.request
        session_store = request.app.state.session_store
        try:
            knowledge_base = request.app.state.knowledge_base_resolver.get(company_id, fallback=False)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": "unknown_company", "detail": "Use widget bootstrap first"},
            )
        self._phrasebook = getattr(knowledge_base, "phrasebook", {})
        lead_service = request.app.state.lead_service
        analytics_service = request.app.state.analytics_service

        session = await session_store.get_or_create(session_id, company_id)
        raw_message = message or ""
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
                service_classifier_payload(request, knowledge_base),
                knowledge_base.company.city,
                knowledge_base.domain_profile,
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
                    company_id=session.company_id,
                    session_id=session.session_id,
                    contact=contact,
                    summary=message,
                    service_id=waiting_policy_result.service_id,
                )
                await lead_service.save(lead)
                await session_store.set_lead_requested(session.session_id, True)
                answer = self._phrase(
                    "lead_success",
                    "Спасибо. Передали ваши контакты менеджеру. С вами свяжутся для уточнения деталей.",
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
                classification = await resolve_classification(message, request, knowledge_base, session)
            policy_result = request.app.state.policy_analyzer(
                message,
                session,
                knowledge_base,
                classification,
            )

        await analytics_service.track_policy_result(
            company_id=session.company_id,
            session_id=session.session_id,
            message=message,
            policy_result=policy_result,
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
                    company_id=session.company_id,
                    session_id=session.session_id,
                    contact=contact,
                    summary=("Заявка на запись: " + message) if is_booking_request else message,
                    service_id=policy_result.service_id,
                )
                await lead_service.save(
                    lead,
                    event_type="booking_created" if is_booking_request else "lead_created",
                )
                lead_created = True
                await session_store.set_lead_requested(session.session_id, True)
                if is_booking_request:
                    answer = self._phrase(
                        "booking_success",
                        "Спасибо. Заявку передали. С вами свяжутся, чтобы подтвердить время и детали.",
                    )
                else:
                    answer = self._phrase(
                        "lead_success",
                        "Спасибо. Передали ваши контакты менеджеру. С вами свяжутся для уточнения деталей.",
                    )
                if not is_booking_request and (session.operator_requested or policy_result.service_id is None):
                    await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
            else:
                answer = str(policy_result.safe_context.get("message_to_user") or "")
        elif policy_result.action == PolicyAction.SMALL_TALK:
            answer = await safe_small_talk(request, knowledge_base.company.company_name, message)
        elif policy_result.action == PolicyAction.OFF_TOPIC:
            answer = str(policy_result.safe_context.get("message_to_user") or "")
        elif policy_result.action == PolicyAction.TRANSFER_OPERATOR:
            await session_store.set_operator_requested(session.session_id, True)
            await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
            await self._enqueue_operator_requested(
                company_id=session.company_id,
                session_id=session.session_id,
                message=message,
            )
            answer = str(
                policy_result.safe_context.get("handoff_message")
                or policy_result.safe_context.get("message_to_user")
                or self._phrase(
                    "handoff_message",
                    "Передаю диалог менеджеру. Оператор увидит историю переписки.",
                )
            )
        elif policy_result.action == PolicyAction.CLARIFY:
            direct_clarify_reasons = {
                PolicyReason.OPERATOR_REQUESTED,
                PolicyReason.LOCATION_MISMATCH,
                PolicyReason.UNSUPPORTED_CITY,
                PolicyReason.UNKNOWN_SERVICE,
                PolicyReason.SIMILAR_SERVICES_FOUND,
                PolicyReason.PRICE_QUESTION_NO_SERVICE,
                PolicyReason.SERVICE_EXPLANATION,
                PolicyReason.BOOKING_REQUEST,
                PolicyReason.CONTACT_PROVIDED,
            }
            if (
                policy_result.reason in direct_clarify_reasons
                or policy_result.safe_context.get("force_direct_answer")
                or policy_result.safe_context.get("message_to_user")
            ):
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
        elif (
            policy_result.action == PolicyAction.ANSWER
            and policy_result.safe_context.get("message_to_user")
            and not policy_result.safe_context.get("question_type")
            and not policy_result.safe_context.get("service")
            and not policy_result.safe_context.get("all_services")
        ):
            answer = str(policy_result.safe_context.get("message_to_user") or "")
        elif should_use_consultation_llm(policy_result.safe_context):
            consultation_risk, _request_id = await classify_consultation_risk(
                request,
                message,
                policy_result.safe_context,
            )
            if consultation_risk == CONSULTATION_RISK_RESTRICTED:
                await analytics_service.track_event(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    event_type="regulated_handoff",
                    message=message,
                    metadata={"source": "consultation_risk_classifier"},
                )
                await analytics_service.track_event(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    event_type="operator_requested",
                    message=message,
                    metadata={"source": "consultation_risk_classifier"},
                )
                await session_store.set_operator_requested(session.session_id, True)
                await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
                await self._enqueue_operator_requested(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    message=message,
                )
                answer = await safe_restricted_handoff(request, message)
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
            quick_actions=format_quick_actions(response_quick_actions, request, knowledge_base),
        )
