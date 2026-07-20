"""сервис обработки сообщений чата."""

import logging
import re

from fastapi import Request
from fastapi.responses import JSONResponse

from ..leads import build_lead_from_contact, classify_lead_reason, lead_trigger_for, recent_messages_for
from ..knowledge import normalize_text
from ..models import (
    ChatMessageResponse,
    ContextFrame,
    MessageRole,
    PendingAction,
    PolicyAction,
    PolicyReason,
    QuickAction,
    SessionStatus,
)
from ..policy import classify_and_extract
from ..policy.constants import NEGATIVE_MESSAGES
from ..policy.extractors import contains_keyword, extract_name, extract_phone
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
from .session_summarizer import summarize_session


logger = logging.getLogger(__name__)


class ChatService:
    """оркестрирует обработку одного пользовательского сообщения."""

    def __init__(self, request: Request) -> None:
        self.request = request

    def _phrase(self, key: str, fallback: str) -> str:
        phrasebook = getattr(self, "_phrasebook", {})
        value = phrasebook.get(key) if isinstance(phrasebook, dict) else None
        return str(value).strip() if value else fallback

    def _domain_profile_value(self, knowledge_base, key: str, fallback: str) -> str:
        domain_profile = getattr(knowledge_base, "domain_profile", {}) or {}
        if not isinstance(domain_profile, dict):
            return fallback
        value = str(domain_profile.get(key) or "").strip()
        return value or fallback

    def _regulated_escalation_mode(self, knowledge_base) -> str:
        mode = self._domain_profile_value(knowledge_base, "regulated_escalation", "soft")
        return mode if mode in {"soft", "instant"} else "soft"

    def _regulated_lead_mode(self, knowledge_base) -> str:
        mode = self._domain_profile_value(knowledge_base, "regulated_lead_mode", "flagged")
        return mode if mode in {"flagged", "normal"} else "flagged"

    def _regulated_lead_metadata(self, knowledge_base) -> dict[str, object]:
        if self._regulated_lead_mode(knowledge_base) == "normal":
            return {
                "needs_operator": False,
                "lead_trigger": "ask_contact",
                "reason": "commercial_interest",
            }
        return {
            "needs_operator": True,
            "lead_trigger": "regulated_advice",
            "reason": "medical_risk",
        }

    def _regulated_soft_quick_actions(self) -> list[dict[str, str]]:
        return [
            {
                "label": "Оставить телефон",
                "type": "message",
                "value": "Хочу оставить телефон",
            },
            {
                "label": "Подключить менеджера",
                "type": "message",
                "value": "Да, менеджера",
            },
        ]

    async def _regulated_soft_offer_response(
        self,
        *,
        session_store,
        session,
        knowledge_base,
    ) -> tuple[PolicyAction, str, list[dict[str, str]]]:
        await session_store.set_pending_action(session.session_id, PendingAction.OFFERED_OPERATOR.value)
        await session_store.update_context(session.session_id, last_intent=PolicyReason.REGULATED_ADVICE.value)
        await session_store.update_contact_draft(
            session.session_id,
            metadata=self._regulated_lead_metadata(knowledge_base),
        )
        answer = self._phrase(
            "regulated_soft_offer",
            (
                "Это лучше обсудить со специалистом. Могу передать ваш контакт менеджеру "
                "или подключить его сейчас. Если вопрос срочный — позвоните нам напрямую или в скорую (103)."
            ),
        )
        return PolicyAction.CLARIFY, answer, self._regulated_soft_quick_actions()

    def _operator_url(self) -> str:
        try:
            base_url = str(self.request.url_for("operator_page"))
        except Exception:
            base_url = "/operator"
        token = getattr(self.request.app.state.settings, "operator_token", "")
        return f"{base_url}?token={token}"

    def _operator_session_url(self, session_id: str) -> str:
        return f"{self._operator_url()}&session_id={session_id}"

    def _looks_like_partial_phone(self, message: str) -> bool:
        digits = re.sub(r"\D", "", message)
        if not digits:
            return False
        if len(digits) >= 11:
            return False
        return len(digits) >= 7 and digits[0] in {"7", "8", "9"}

    def _pending_contact_answer(self, session, message: str) -> str | None:
        phone = extract_phone(message)
        if phone:
            return None
        if self._looks_like_partial_phone(message):
            return "Похоже, номер неполный. Проверьте, пожалуйста, и отправьте телефон ещё раз."
        name = extract_name(message, None)
        if name:
            return f"{name}, напишите, пожалуйста, телефон — передам заявку менеджеру."
        return "Напишите, пожалуйста, телефон. Можно просто номер и имя одним сообщением."

    def _last_user_message_before_current(self, session, current_message: str) -> str | None:
        user_messages = [
            str(stored_message.text or "").strip()
            for stored_message in session.messages
            if stored_message.role == MessageRole.USER and str(stored_message.text or "").strip()
        ]
        if len(user_messages) < 2:
            return None
        prior_message = user_messages[-2]
        if not prior_message or prior_message == current_message.strip():
            return None
        if extract_phone(prior_message):
            return None
        return prior_message

    def _lead_summary(self, session, message: str, *, is_booking_request: bool) -> str:
        prefix = "Заявка на запись: " if is_booking_request else ""
        prior_message = self._last_user_message_before_current(session, message)
        if prior_message:
            return f"{prefix}{prior_message} | Контакт: {message}"
        return f"{prefix}{message}"

    def _unresolved_lead_metadata(
        self,
        session,
        current_message: str,
        *,
        last_intent: str | None = None,
    ) -> dict[str, object]:
        intent = last_intent if last_intent is not None else session.last_intent
        if intent not in {
            PolicyReason.UNKNOWN_SERVICE.value,
            PolicyReason.SIMILAR_SERVICES_FOUND.value,
        }:
            return {}
        unresolved_query = self._last_user_message_before_current(session, current_message)
        if not unresolved_query:
            return {}
        return {
            "unresolved_service_mention": True,
            "unresolved_query": unresolved_query,
            "lead_trigger": "unknown_service",
            "reason": "unknown_service",
        }

    def _current_unresolved_lead_metadata(self, policy_result, current_message: str) -> dict[str, object]:
        safe_context = getattr(policy_result, "safe_context", {}) or {}
        if not safe_context.get("service_unresolved"):
            return {}
        unresolved_query = str(safe_context.get("unresolved_query") or "").strip() or current_message.strip()
        if not unresolved_query:
            return {}
        return {
            "unresolved_service_mention": True,
            "unresolved_query": unresolved_query,
            "lead_trigger": "unknown_service",
            "reason": "unknown_service",
        }

    async def _finalize_lead_summary(self, session, lead) -> None:
        if lead.lead_trigger == "unknown_service" and lead.unresolved_query:
            lead.summary = (
                f"Пользователь спрашивал неподтверждённую услугу: «{lead.unresolved_query}». "
                "Оставил контакт."
            )
            return
        llm_client = getattr(self.request.app.state, "llm_client", None)
        if llm_client is None:
            return
        lead.summary = await summarize_session(llm_client, session=session, lead=lead)

    async def _clear_contact_state(self, session_store, session_id: str) -> None:
        await session_store.set_pending_action(session_id, None)
        await session_store.update_contact_draft(session_id, clear=True)

    def _looks_like_new_question(self, message: str, knowledge_base) -> bool:
        """отличает смену темы от попытки дать имя/телефон.

        Без этого любой новый вопрос ("какие врачи у вас есть?") молча трактовался
        как неудачная попытка назвать имя, и extract_name вытаскивал случайное слово
        ("Какие") — бот застревал в бесконечном "напишите телефон" на любое сообщение.
        """

        if "?" in message:
            return True
        classification = classify_and_extract(
            message,
            service_classifier_payload(self.request, knowledge_base),
            knowledge_base.company.city,
            knowledge_base.domain_profile,
        )
        return float(classification.get("confidence") or 0.0) > 0

    async def _handle_booking_service_selection(
        self,
        *,
        session_store,
        session,
        message: str,
        knowledge_base,
    ) -> ChatMessageResponse | None:
        if session.pending_action != PendingAction.BOOKING_CONTACT.value:
            return None

        classification = classify_and_extract(
            message,
            service_classifier_payload(self.request, knowledge_base),
            knowledge_base.company.city,
            knowledge_base.domain_profile,
        )
        service = knowledge_base.find_service_by_id(classification.get("service_id"))
        if service is None:
            return None

        await session_store.update_context(
            session.session_id,
            last_service_id=service.id,
            last_intent=PolicyReason.BOOKING_REQUEST.value,
        )
        answer = self._phrase(
            "booking_contact_prompt",
            "Чтобы оставить заявку, напишите имя, телефон и удобное время. Мы передадим заявку, а менеджер подтвердит детали.",
        )
        await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
        session = await session_store.get(session.session_id)
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.CLARIFY,
            answer=answer,
            lead_created=False,
            quick_actions=[],
        )

    async def _handle_pending_contact(
        self,
        *,
        session_store,
        lead_service,
        session,
        message: str,
        knowledge_base,
    ) -> ChatMessageResponse | None:
        if session.pending_action not in {
            PendingAction.COLLECT_CONTACT.value,
            PendingAction.BOOKING_CONTACT.value,
        }:
            return None

        normalized_message = normalize_text(message)
        if contains_keyword(normalized_message, NEGATIVE_MESSAGES):
            was_booking_request = session.pending_action == PendingAction.BOOKING_CONTACT.value
            await self._clear_contact_state(session_store, session.session_id)
            if was_booking_request:
                answer = self._phrase(
                    "booking_cancelled",
                    "Ок, заявку не оформляем. Могу подсказать по услугам, ценам или позвать менеджера.",
                )
            else:
                answer = self._phrase(
                    "contact_cancelled",
                    "Ок, контакт не оставляем. Могу подсказать по услугам, ценам или позвать менеджера.",
                )
            await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
            session = await session_store.get(session.session_id)
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=PolicyAction.CLARIFY,
                answer=answer,
                lead_created=False,
                quick_actions=[],
            )

        phone = extract_phone(message)
        if not phone:
            booking_service_response = await self._handle_booking_service_selection(
                session_store=session_store,
                session=session,
                message=message,
                knowledge_base=knowledge_base,
            )
            if booking_service_response is not None:
                return booking_service_response

        if not phone and self._looks_like_new_question(message, knowledge_base):
            await self._clear_contact_state(session_store, session.session_id)
            return None

        if not phone:
            answer = self._pending_contact_answer(session, message)
            name = extract_name(message, None)
            if name:
                await session_store.update_contact_draft(session.session_id, name=name)
            await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
            session = await session_store.get(session.session_id)
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=PolicyAction.CLARIFY,
                answer=answer,
                lead_created=False,
                quick_actions=[],
            )

        name = extract_name(message, phone) or str(session.contact_draft.get("name") or "").strip() or None
        await session_store.update_contact_draft(session.session_id, name=name, phone=phone)

        is_booking_request = session.pending_action == PendingAction.BOOKING_CONTACT.value
        draft_needs_operator = bool(session.contact_draft.get("needs_operator"))
        draft_lead_trigger = str(session.contact_draft.get("lead_trigger") or "").strip()
        draft_reason = str(session.contact_draft.get("reason") or "").strip()
        draft_unresolved_query = str(session.contact_draft.get("unresolved_query") or "").strip()
        contact = {"name": name, "phone": phone}
        lead = build_lead_from_contact(
            company_id=session.company_id,
            session_id=session.session_id,
            contact=contact,
            summary=self._lead_summary(session, message, is_booking_request=is_booking_request),
            service_id=session.last_service_id,
            reason=draft_reason
            or classify_lead_reason(last_intent=session.last_intent, is_booking_request=is_booking_request),
            needs_operator=draft_needs_operator or session.operator_requested,
            lead_trigger=draft_lead_trigger
            or lead_trigger_for(
                is_booking_request=is_booking_request,
                is_operator_flow=session.operator_requested,
                is_regulated_flow=draft_needs_operator,
            ),
            unresolved_query=draft_unresolved_query,
            recent_messages=recent_messages_for(session),
            operator_url=self._operator_session_url(session.session_id),
        )
        await self._finalize_lead_summary(session, lead)
        await lead_service.save(
            lead,
            event_type="booking_created" if is_booking_request else "lead_created",
        )
        await session_store.set_lead_requested(session.session_id, True)
        await self._clear_contact_state(session_store, session.session_id)

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
        await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
        session = await session_store.get(session.session_id)
        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=PolicyAction.ASK_CONTACT,
            answer=answer,
            lead_created=True,
            quick_actions=[],
        )

    async def _remember_policy_context(self, session_store, session, policy_result) -> None:
        last_intent = str(policy_result.reason.value if hasattr(policy_result.reason, "value") else policy_result.reason)
        active_frame = self._context_frame_from_policy_result(session, policy_result, last_intent)
        prior_unresolved_metadata = self._unresolved_lead_metadata(session, "")
        await session_store.update_context(
            session.session_id,
            last_service_id=policy_result.service_id,
            last_intent=last_intent,
            active_frame=active_frame,
            clear_active_frame=active_frame is None and policy_result.action in {PolicyAction.OFF_TOPIC, PolicyAction.REJECT},
        )

        if (
            policy_result.safe_context.get("contact_request_cancelled")
            or policy_result.safe_context.get("booking_request_cancelled")
            or policy_result.safe_context.get("general_cancelled")
            or policy_result.safe_context.get("contact")
            or policy_result.action == PolicyAction.TRANSFER_OPERATOR
        ):
            await self._clear_contact_state(session_store, session.session_id)
            return

        if policy_result.action == PolicyAction.ASK_CONTACT and not policy_result.safe_context.get("contact"):
            pending_action = (
                PendingAction.BOOKING_CONTACT.value
                if policy_result.safe_context.get("booking_request")
                else PendingAction.COLLECT_CONTACT.value
            )
            await session_store.set_pending_action(session.session_id, pending_action)
            if prior_unresolved_metadata:
                await session_store.update_contact_draft(session.session_id, metadata=prior_unresolved_metadata)
            return

        if policy_result.action == PolicyAction.CLARIFY and policy_result.safe_context.get("booking_request"):
            await session_store.set_pending_action(session.session_id, PendingAction.BOOKING_CONTACT.value)
            if prior_unresolved_metadata:
                await session_store.update_contact_draft(session.session_id, metadata=prior_unresolved_metadata)
            return

        if policy_result.action == PolicyAction.CLARIFY and policy_result.reason == PolicyReason.OPERATOR_REQUESTED:
            await session_store.set_pending_action(session.session_id, PendingAction.OFFERED_OPERATOR.value)
            return

        if policy_result.action in {PolicyAction.ANSWER, PolicyAction.SMALL_TALK, PolicyAction.OFF_TOPIC}:
            await self._clear_contact_state(session_store, session.session_id)

    def _context_frame_from_policy_result(self, session, policy_result, last_intent: str) -> ContextFrame | None:
        safe_context = policy_result.safe_context or {}
        service = safe_context.get("service") if isinstance(safe_context.get("service"), dict) else None
        service_id = policy_result.service_id or (str(service.get("id") or "").strip() if service else None)
        service_name = str(service.get("name") or "").strip() if service else None
        expires_at_turn = session.message_count + 5

        fact_guard = safe_context.get("fact_guard") if isinstance(safe_context.get("fact_guard"), dict) else None
        if fact_guard is not None:
            return ContextFrame(
                frame_type="fact_question",
                entity_type="service" if service_id else None,
                entity_id=service_id,
                entity_label=service_name,
                slots={
                    "topic": str(fact_guard.get("topic") or "").strip(),
                    "question_type": safe_context.get("question_type"),
                },
                last_intent=last_intent,
                expires_at_turn=expires_at_turn,
            )

        clinic_info_topic = str(safe_context.get("clinic_info_topic") or "").strip()
        if clinic_info_topic:
            return ContextFrame(
                frame_type="clinic_info",
                slots={"topic": clinic_info_topic},
                last_intent=last_intent,
                expires_at_turn=expires_at_turn,
            )

        if service_id:
            slots = {"question_type": safe_context.get("question_type")}
            variant_matches = safe_context.get("variant_matches")
            if isinstance(variant_matches, list) and variant_matches and isinstance(variant_matches[0], dict):
                slots["variant"] = variant_matches[0]
            return ContextFrame(
                frame_type="service_interest",
                entity_type="service",
                entity_id=service_id,
                entity_label=service_name,
                slots=slots,
                last_intent=last_intent,
                expires_at_turn=expires_at_turn,
            )

        return None

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
                answer=self._phrase("empty_message", "Похоже, сообщение пустое. Напишите вопрос, и я подскажу."),
                lead_created=False,
                quick_actions=[],
            )

        if not HAS_LETTER_OR_DIGIT.search(stripped_message):
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=PolicyAction.CLARIFY,
                answer=self._phrase(
                    "empty_message_letters",
                    "Не совсем понял вопрос. Можете переформулировать словами?",
                ),
                lead_created=False,
                quick_actions=[],
            )

        if session.message_count >= MAX_SESSION_MESSAGES:
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=PolicyAction.CLARIFY,
                answer=self._phrase("rate_limit", RATE_LIMIT_ANSWER),
                lead_created=False,
                quick_actions=[
                    QuickAction(label="Начать новый диалог", type="message", value="Начать новый диалог")
                ],
            )

        message = stripped_message[:MAX_MESSAGE_LENGTH]
        session = await session_store.append_message(session.session_id, MessageRole.USER, message) or session

        pending_contact_response = await self._handle_pending_contact(
            session_store=session_store,
            lead_service=lead_service,
            session=session,
            message=message,
            knowledge_base=knowledge_base,
        )
        if pending_contact_response is not None:
            return pending_contact_response

        if not extract_phone(message) and self._looks_like_partial_phone(message):
            answer = "Похоже, номер неполный. Проверьте, пожалуйста, и отправьте телефон ещё раз."
            await session_store.append_message(session.session_id, MessageRole.ASSISTANT, answer)
            session = await session_store.get(session.session_id)
            return ChatMessageResponse(
                session_id=session.session_id,
                status=session.status,
                action=PolicyAction.CLARIFY,
                answer=answer,
                lead_created=False,
                quick_actions=[],
            )

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
                unresolved_metadata = self._current_unresolved_lead_metadata(
                    waiting_policy_result,
                    message,
                ) or self._unresolved_lead_metadata(
                    session,
                    message,
                    last_intent=session.last_intent,
                )
                lead = build_lead_from_contact(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    contact=contact,
                    summary=self._lead_summary(session, message, is_booking_request=False),
                    service_id=waiting_policy_result.service_id,
                    reason=str(unresolved_metadata.get("reason") or "")
                    or classify_lead_reason(last_intent=session.last_intent, is_booking_request=False),
                    needs_operator=True,
                    lead_trigger=str(unresolved_metadata.get("lead_trigger") or "")
                    or lead_trigger_for(is_booking_request=False, is_operator_flow=True),
                    unresolved_query=str(unresolved_metadata.get("unresolved_query") or ""),
                    recent_messages=recent_messages_for(session),
                    operator_url=self._operator_session_url(session.session_id),
                )
                await self._finalize_lead_summary(session, lead)
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
                answer=self._phrase(
                    "human_active_wait",
                    "Чат передан менеджеру. Пожалуйста, дождитесь ответа.",
                ),
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
        prior_last_intent = session.last_intent
        prior_contact_draft = dict(session.contact_draft)
        await self._remember_policy_context(session_store, session, policy_result)

        lead_created = False
        answer = ""
        response_action = policy_result.action
        response_quick_actions = policy_result.quick_actions

        if policy_result.action == PolicyAction.ASK_CONTACT:
            contact = policy_result.safe_context.get("contact")
            if contact:
                is_booking_request = bool(policy_result.safe_context.get("booking_request"))
                draft_needs_operator = bool(prior_contact_draft.get("needs_operator"))
                draft_lead_trigger = str(prior_contact_draft.get("lead_trigger") or "").strip()
                draft_reason = str(prior_contact_draft.get("reason") or "").strip()
                draft_unresolved_query = str(prior_contact_draft.get("unresolved_query") or "").strip()
                unresolved_metadata = self._current_unresolved_lead_metadata(
                    policy_result,
                    message,
                ) or self._unresolved_lead_metadata(
                    session,
                    message,
                    last_intent=prior_last_intent,
                )
                unresolved_lead_trigger = str(unresolved_metadata.get("lead_trigger") or "").strip()
                unresolved_reason = str(unresolved_metadata.get("reason") or "").strip()
                unresolved_query = str(unresolved_metadata.get("unresolved_query") or "").strip()
                lead = build_lead_from_contact(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    contact=contact,
                    summary=self._lead_summary(session, message, is_booking_request=is_booking_request),
                    service_id=policy_result.service_id or session.last_service_id,
                    reason=draft_reason
                    or unresolved_reason
                    or classify_lead_reason(last_intent=prior_last_intent, is_booking_request=is_booking_request),
                    needs_operator=draft_needs_operator or session.operator_requested,
                    lead_trigger=draft_lead_trigger
                    or unresolved_lead_trigger
                    or lead_trigger_for(
                        is_booking_request=is_booking_request,
                        is_operator_flow=session.operator_requested,
                        is_regulated_flow=draft_needs_operator,
                    ),
                    unresolved_query=draft_unresolved_query or unresolved_query,
                    recent_messages=recent_messages_for(session),
                    operator_url=self._operator_session_url(session.session_id),
                )
                await self._finalize_lead_summary(session, lead)
                await lead_service.save(
                    lead,
                    event_type="booking_created" if is_booking_request else "lead_created",
                )
                lead_created = True
                await session_store.set_lead_requested(session.session_id, True)
                await self._clear_contact_state(session_store, session.session_id)
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
                if not is_booking_request and session.operator_requested:
                    await session_store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
            else:
                answer = str(policy_result.safe_context.get("message_to_user") or "")
        elif policy_result.action == PolicyAction.SMALL_TALK:
            answer = await safe_small_talk(request, knowledge_base.company.company_name, message)
        elif policy_result.action == PolicyAction.OFF_TOPIC:
            answer = str(policy_result.safe_context.get("message_to_user") or "")
        elif policy_result.action == PolicyAction.TRANSFER_OPERATOR:
            if (
                policy_result.reason == PolicyReason.REGULATED_ADVICE
                and self._regulated_escalation_mode(knowledge_base) == "soft"
            ):
                response_action, answer, response_quick_actions = await self._regulated_soft_offer_response(
                    session_store=session_store,
                    session=session,
                    knowledge_base=knowledge_base,
                )
            else:
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
                        "Передаю диалог менеджеру. Он увидит историю переписки.",
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
            and (
                policy_result.safe_context.get("force_direct_answer")
                or (
                    not policy_result.safe_context.get("question_type")
                    and not policy_result.safe_context.get("service")
                    and not policy_result.safe_context.get("all_services")
                )
            )
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
                if self._regulated_escalation_mode(knowledge_base) == "soft":
                    response_action, answer, response_quick_actions = await self._regulated_soft_offer_response(
                        session_store=session_store,
                        session=session,
                        knowledge_base=knowledge_base,
                    )
                else:
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

        answer_kind = "handoff" if response_action == PolicyAction.TRANSFER_OPERATOR else None
        await session_store.append_message(
            session.session_id, MessageRole.ASSISTANT, answer, kind=answer_kind
        )
        session = await session_store.get(session.session_id)

        return ChatMessageResponse(
            session_id=session.session_id,
            status=session.status,
            action=response_action,
            answer=answer,
            lead_created=lead_created,
            quick_actions=format_quick_actions(response_quick_actions, request, knowledge_base),
        )
