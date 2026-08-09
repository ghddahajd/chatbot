"""сервис обработки сообщений чата."""

import asyncio
import logging
import re

from fastapi import Request
from fastapi.responses import JSONResponse

from ..delivery import _escape_markdown
from ..leads import build_lead_from_contact, classify_lead_reason, lead_trigger_for, recent_messages_for
from ..telegram_bridge import client_label_for_session
from ..knowledge import normalize_text, phrasebook_value_to_text
from ..models import (
    ChatMessageResponse,
    ContextFrame,
    MessageRole,
    PendingAction,
    PolicyAction,
    PolicyReason,
    SessionStatus,
)
from ..policy import classify_and_extract, undisclosed_equipment_terms
from ..policy.constants import NEGATIVE_MESSAGES, PHONE_PATTERN
from ..policy.extractors import contains_keyword, extract_name, extract_phone
from ..routes.chat_utils import (
    CONSULTATION_RISK_RESTRICTED,
    HAS_LETTER_OR_DIGIT,
    MAX_MESSAGE_LENGTH,
    MAX_SESSION_MESSAGES,
    RATE_LIMIT_ANSWER,
    _objection_classification,
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
from ..validator import validate_article_guidance_response
from .session_summarizer import summarize_session


logger = logging.getLogger(__name__)


ENGAGEMENT_OFFER_THRESHOLDS = (5, 8, 13)
ENGAGEMENT_OFFER_KEYS = ("engagement_offer_1", "engagement_offer_2", "engagement_offer_3")
ENGAGEMENT_DISMISS_MESSAGES = {
    "продолжить тут",
    "продолжить здесь",
    "продолжим тут",
    "продолжим здесь",
}
LEAD_CONTEXT_START_KEY = "lead_context_start_index"
LEAD_SERVICE_ID_KEY = "lead_service_id"
PREFERRED_TIME_KEY = "preferred_time"


class ChatService:
    """оркестрирует обработку одного пользовательского сообщения."""

    def __init__(self, request: Request) -> None:
        self.request = request

    def _phrase(self, key: str, fallback: str, seed: str | None = None) -> str:
        phrasebook = getattr(self, "_phrasebook", {})
        value = phrasebook.get(key) if isinstance(phrasebook, dict) else None
        return phrasebook_value_to_text(value, fallback, seed=seed)

    def _is_substantive_policy_result(self, policy_result) -> bool:
        return policy_result.action not in {
            PolicyAction.SMALL_TALK,
            PolicyAction.OFF_TOPIC,
            PolicyAction.REJECT,
        }

    async def _article_guidance_answer(
        self,
        *,
        message: str,
        safe_context: dict[str, object],
        history,
    ) -> str:
        candidate = safe_context.get("article_guidance_candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        fallback = str(candidate.get("fallback_message_to_user") or safe_context.get("message_to_user") or "").strip()
        excerpt = str(candidate.get("excerpt") or "").strip()
        if not fallback or not excerpt:
            return fallback

        llm_context = dict(safe_context)
        llm_context["question_type"] = "article_guidance_excerpt"
        llm_context["article_guidance_candidate"] = candidate
        llm_context["article_context"] = [
            {
                "title": str(candidate.get("title") or ""),
                "url": str(candidate.get("url") or ""),
                "snippet": excerpt,
            }
        ]
        llm_context.pop("message_to_user", None)

        answer = await safe_complete(self.request, llm_context, message, history)
        if validate_article_guidance_response(answer, llm_context):
            return answer
        return fallback

    def _engagement_offer_text(self, offer_index: int) -> str:
        fallback_texts = (
            "Вижу, диалог уже длинный — хотите, чтобы дальше подключился администратор, или продолжим здесь?",
            "Если удобнее — могу передать администратору краткое резюме нашего разговора, он подключится быстрее.",
            "Ещё раз на всякий случай предложу: подключить администратора, чтобы не тянуть?",
        )
        return self._phrase(ENGAGEMENT_OFFER_KEYS[offer_index], fallback_texts[offer_index])

    def _engagement_offer_quick_actions(self) -> list[dict[str, str]]:
        return [
            {
                "label": "Передать администратору",
                "type": "message",
                "value": "Передать администратору",
            },
            {
                "label": "Продолжить тут",
                "type": "message",
                "value": "Продолжить тут",
            },
        ]

    def _is_engagement_dismiss_message(self, message: str) -> bool:
        return normalize_text(message) in ENGAGEMENT_DISMISS_MESSAGES

    async def _engagement_dismiss_response(self, session_store, session) -> ChatMessageResponse:
        answer = self._phrase(
            "engagement_continue",
            "Хорошо, продолжим здесь. Напишите, что хотите уточнить дальше.",
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

    async def _apply_engagement_offer_if_due(
        self,
        *,
        session_store,
        session,
        answer: str,
        quick_actions,
    ) -> tuple[str, list[dict[str, str]] | object]:
        if session.status != SessionStatus.AI_ACTIVE:
            return answer, quick_actions
        if session.pending_action or session.lead_requested or session.operator_requested:
            return answer, quick_actions
        offer_count = max(0, min(int(session.engagement_offer_count or 0), len(ENGAGEMENT_OFFER_THRESHOLDS)))
        if offer_count >= len(ENGAGEMENT_OFFER_THRESHOLDS):
            return answer, quick_actions
        threshold = ENGAGEMENT_OFFER_THRESHOLDS[offer_count]
        if int(session.substantive_message_count or 0) < threshold:
            return answer, quick_actions

        offer_text = self._engagement_offer_text(offer_count)
        updated_answer = f"{answer.rstrip()}\n\n{offer_text}" if answer.strip() else offer_text
        await session_store.update_context(
            session.session_id,
            engagement_offer_count=offer_count + 1,
        )
        return updated_answer, self._engagement_offer_quick_actions()

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

    def _referral_quick_action(self, referral_service: object) -> dict[str, str] | None:
        if not isinstance(referral_service, dict):
            return None
        service_name = str(referral_service.get("name") or "").strip()
        if not service_name:
            return None
        return {"label": service_name, "type": "message", "value": service_name}

    async def _regulated_soft_offer_response(
        self,
        *,
        session_store,
        session,
        knowledge_base,
        referral_service: object = None,
        urgent: bool = True,
    ) -> tuple[PolicyAction, str, list[dict[str, str]]]:
        await session_store.set_pending_action(session.session_id, PendingAction.OFFERED_OPERATOR.value)
        await session_store.update_context(session.session_id, last_intent=PolicyReason.REGULATED_ADVICE.value)
        await session_store.update_contact_draft(
            session.session_id,
            metadata=self._regulated_lead_metadata(knowledge_base),
        )
        # "Если срочно — звоните... в скорую (103)" уместно для реально острых сигналов
        # (кровотечение, аллергия), но не для бытовых вопросов вроде "а больно?" — см.
        # escalation_urgency в _medical_referral_result (policy/__init__.py). По умолчанию
        # urgent=True (безопасный дефолт для путей без явной оценки срочности, например
        # LLM-классификатора риска на consultation-ветке).
        phrase_key = "regulated_soft_offer" if urgent else "regulated_soft_offer_calm"
        answer = self._phrase(
            phrase_key,
            (
                "Это лучше обсудить со специалистом. Могу передать ваш контакт менеджеру "
                "или подключить его сейчас. Если вопрос срочный — позвоните нам напрямую или в скорую (103)."
            )
            if urgent
            else (
                "Заочно тут лучше не гадать — это уточнит специалист. Могу передать ваш контакт "
                "менеджеру или подключить его сейчас."
            ),
            seed=f"{session.session_id}:regulated_soft_offer:{session.message_count}",
        )
        quick_actions = self._regulated_soft_quick_actions()
        referral_action = self._referral_quick_action(referral_service)
        if referral_action is not None:
            quick_actions = [referral_action, *quick_actions]
        return PolicyAction.CLARIFY, answer, quick_actions

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

    _TIME_PREFERENCE_MARKERS = {"утром": "утром", "утро": "утром", "вечером": "вечером", "вечер": "вечером"}

    def _extract_time_preference(self, message: str) -> str | None:
        # §2/§3.3 скрипта: assumptive close "утро или вечер?" — квик-экшены той же формы
        # (booking_contact_prompt). Только короткий явный ответ на этот конкретный выбор,
        # не любое упоминание "утром"/"вечером" внутри произвольного предложения — там это
        # может значить что угодно другое (например "были у вас утром на чистке").
        normalized = normalize_text(message)
        tokens = normalized.split()
        if len(tokens) > 3:
            return None
        for token in tokens:
            if token in self._TIME_PREFERENCE_MARKERS:
                return self._TIME_PREFERENCE_MARKERS[token]
        return None

    def _pending_contact_answer(
        self, session, message: str, knowledge_base, *, time_preference: str | None = None
    ) -> str | None:
        phone = extract_phone(message)
        if phone:
            return None
        if self._looks_like_partial_phone(message):
            return "Похоже, номер неполный. Проверьте, пожалуйста, и отправьте телефон ещё раз."
        if time_preference:
            return f"Хорошо, {time_preference}. Напишите, пожалуйста, имя и телефон — передам заявку менеджеру."
        name = extract_name(message, None, known_services=knowledge_base.services)
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

    def _contact_message_remainder(self, message: str, *, name: str | None, phone: str | None) -> str:
        remainder = PHONE_PATTERN.sub(" ", message)
        if phone:
            remainder = remainder.replace(phone, " ")
            remainder = remainder.replace(phone.removeprefix("+"), " ")
        if name:
            remainder = re.sub(
                rf"(?<!\w){re.escape(name)}(?!\w)",
                " ",
                remainder,
                flags=re.IGNORECASE,
            )
        remainder = re.sub(r"[\d+\-\(\)]", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder)
        return remainder.strip(" ,;:.!?|")

    def _lead_summary(
        self,
        session,
        message: str,
        *,
        is_booking_request: bool,
        name: str | None = None,
        phone: str | None = None,
    ) -> str:
        prefix = "Заявка на запись: " if is_booking_request else ""
        prior_message = self._last_user_message_before_current(session, message)
        if prior_message:
            details = self._contact_message_remainder(message, name=name, phone=phone)
            details_suffix = f" ({details})" if details else ""
            return f"{prefix}{prior_message}{details_suffix}"
        details = self._contact_message_remainder(message, name=name, phone=phone)
        if details:
            return f"{prefix}{details}"
        if prefix:
            return prefix.rstrip(": ")
        return f"{prefix}{message}"

    def _lead_context_start_metadata(self, session) -> dict[str, int]:
        return {LEAD_CONTEXT_START_KEY: max(0, len(session.messages) - 1)}

    def _lead_context_metadata(self, session, service_id: str | None = None) -> dict[str, object]:
        metadata: dict[str, object] = self._lead_context_start_metadata(session)
        if service_id:
            metadata[LEAD_SERVICE_ID_KEY] = service_id
        return metadata

    def _lead_context_start_index(self, session, draft: dict[str, object] | None) -> int:
        if isinstance(draft, dict):
            try:
                value = int(draft.get(LEAD_CONTEXT_START_KEY, ""))
            except (TypeError, ValueError):
                value = -1
            if 0 <= value < len(session.messages):
                return value
        return max(0, len(session.messages) - 1)

    def _lead_context_session(self, session, start_index: int):
        if start_index <= 0:
            return session
        return session.model_copy(update={"messages": session.messages[start_index:]})

    def _lead_service_id(
        self,
        session,
        *,
        is_booking_request: bool,
        policy_service_id: str | None = None,
        draft: dict[str, object] | None = None,
    ) -> str | None:
        if policy_service_id:
            return policy_service_id
        if is_booking_request:
            value = draft.get(LEAD_SERVICE_ID_KEY) if isinstance(draft, dict) else None
            if value is None:
                return None
            return str(value).strip() or None
        return session.last_service_id

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

    def _notable_facts_block(self, session, lead_summary: str) -> str:
        # Живой баг: локальная LLM-саммаризация нередко теряет предпочтение по времени и
        # другие заметные моменты разговора (жалоба, деликатная тема, возражение) даже когда
        # они явно есть в истории — не полагаемся на модель для деталей, которые должны дойти
        # до оператора гарантированно. Отдельная секция, не вклеено в прозу пересказа — так
        # оператор сразу видит, что это проверенный факт, а не интерпретация LLM.
        summary_lower = lead_summary.lower()
        facts: list[str] = []
        preferred_time = str(session.contact_draft.get(PREFERRED_TIME_KEY) or "").strip()
        if preferred_time and preferred_time not in summary_lower:
            facts.append(f"Предпочитает: {preferred_time}")
        for flag in session.notable_flags:
            if flag.lower() not in summary_lower:
                facts.append(flag)
        if not facts:
            return ""
        bullet_list = "\n".join(f"— {fact}" for fact in facts)
        return f"\n\n📌 Из переписки:\n{bullet_list}"

    async def _finalize_lead_summary(self, session, lead) -> None:
        if lead.lead_trigger == "unknown_service" and lead.unresolved_query:
            lead.summary = (
                f"Пользователь спрашивал неподтверждённую услугу: «{lead.unresolved_query}». "
                "Оставил контакт."
            )
            lead.summary += self._notable_facts_block(session, lead.summary)
            return
        llm_client = getattr(self.request.app.state, "llm_client", None)
        if llm_client is None:
            lead.summary += self._notable_facts_block(session, lead.summary)
            return
        lead.summary = await summarize_session(llm_client, session=session, lead=lead)
        lead.summary += self._notable_facts_block(session, lead.summary)

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
        # Живой баг: "я подумаю"/"дорого"/"дешевле в другом месте" во время сбора контакта
        # (pending_action) не содержат "?" и не матчят базовый локальный классификатор
        # (confidence 0.0) — тонули в генерическом "напишите телефон" вместо ответа на
        # возражение. _objection_classification — тот же детерминированный слой, что уже
        # используется в policy для возражений вне этого состояния.
        if _objection_classification(message) is not None:
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
        await session_store.update_contact_draft(
            session.session_id,
            metadata={LEAD_SERVICE_ID_KEY: service.id},
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
            # §3.5 скрипта: "подумаю" ИМЕННО на этапе записи должно дойти до policy с
            # pending_action ещё нетронутым (см. _objection_result) — очистка контакта здесь
            # стёрла бы BOOKING_CONTACT раньше, чем policy успеет его увидеть, и ответ
            # откатится на общий "что смущает" вместо "придержу интерес без обязательств".
            # Дальше, если пациент всё же даст телефон, запись всё равно оформится штатно.
            objection = _objection_classification(message)
            is_booking_hesitation = (
                objection is not None
                and objection.get("context_topic") == "hesitation"
                and session.pending_action == PendingAction.BOOKING_CONTACT.value
            )
            if not is_booking_hesitation:
                await self._clear_contact_state(session_store, session.session_id)
            return None

        if not phone:
            time_preference = self._extract_time_preference(message)
            answer = self._pending_contact_answer(
                session, message, knowledge_base, time_preference=time_preference
            )
            name = extract_name(message, None, known_services=knowledge_base.services)
            if name:
                await session_store.update_contact_draft(session.session_id, name=name)
            if time_preference:
                await session_store.update_contact_draft(
                    session.session_id, metadata={PREFERRED_TIME_KEY: time_preference}
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

        name = (
            extract_name(message, phone, known_services=knowledge_base.services)
            or str(session.contact_draft.get("name") or "").strip()
            or None
        )
        await session_store.update_contact_draft(session.session_id, name=name, phone=phone)

        is_booking_request = session.pending_action == PendingAction.BOOKING_CONTACT.value
        draft_needs_operator = bool(session.contact_draft.get("needs_operator"))
        draft_lead_trigger = str(session.contact_draft.get("lead_trigger") or "").strip()
        draft_reason = str(session.contact_draft.get("reason") or "").strip()
        draft_unresolved_query = str(session.contact_draft.get("unresolved_query") or "").strip()
        lead_context_start_index = self._lead_context_start_index(session, session.contact_draft)
        lead_session = self._lead_context_session(session, lead_context_start_index)
        contact = {"name": name, "phone": phone}
        lead = build_lead_from_contact(
            company_id=session.company_id,
            session_id=session.session_id,
            contact=contact,
            summary=self._lead_summary(
                lead_session,
                message,
                is_booking_request=is_booking_request,
                name=name,
                phone=phone,
            ),
            service_id=self._lead_service_id(
                session,
                is_booking_request=is_booking_request,
                draft=session.contact_draft,
            ),
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
            recent_messages=recent_messages_for(lead_session),
            operator_url=self._operator_session_url(session.session_id),
        )
        await self._finalize_lead_summary(lead_session, lead)
        await lead_service.save(
            lead,
            event_type="booking_created" if is_booking_request else "lead_created",
        )
        await session_store.set_lead_requested(session.session_id, True)
        await self._clear_contact_state(session_store, session.session_id)
        await self._notify_telegram_for_lead(
            lead,
            reason="📅 Новая запись" if is_booking_request else "🔔 Новый лид",
        )

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

    _OBJECTION_FLAG_LABELS = {
        "price": "Возражение: цена",
        "competitor": "Возражение: дешевле в другой клинике",
        "guarantee": "Возражение: просил гарантию результата",
        "pain_fear": "Возражение: боится боли/побочных эффектов",
    }

    def _notable_flag_for_policy_result(self, policy_result) -> str | None:
        # Копим то, что уже надёжно распознала policy по ходу разговора — не пересканируем
        # сырой текст заново. Список узкий и намеренно: не любое возражение/тема, только то,
        # что реально меняет, как оператору стоит заходить в разговор.
        if policy_result.reason == PolicyReason.COMPLAINT:
            return "Была жалоба/угроза негативным отзывом"
        if policy_result.safe_context.get("sensitive_handling"):
            return "Затрагивалась деликатная тема"
        if policy_result.reason == PolicyReason.OBJECTION_HANDLED:
            topic = str(policy_result.safe_context.get("objection_topic") or "")
            return self._OBJECTION_FLAG_LABELS.get(topic)
        return None

    async def _remember_policy_context(self, session_store, session, policy_result) -> None:
        last_intent = str(policy_result.reason.value if hasattr(policy_result.reason, "value") else policy_result.reason)
        active_frame = self._context_frame_from_policy_result(session, policy_result, last_intent)
        prior_unresolved_metadata = self._unresolved_lead_metadata(session, "")
        substantive_message_count = None
        if self._is_substantive_policy_result(policy_result):
            substantive_message_count = int(session.substantive_message_count or 0) + 1
        objection_response_count = None
        if policy_result.reason in {PolicyReason.OBJECTION_HANDLED, PolicyReason.OBJECTION_BACKOFF}:
            objection_response_count = int(session.objection_response_count or 0) + 1
        await session_store.update_context(
            session.session_id,
            last_service_id=policy_result.service_id,
            last_intent=last_intent,
            active_frame=active_frame,
            clear_active_frame=active_frame is None and policy_result.action in {PolicyAction.OFF_TOPIC, PolicyAction.REJECT},
            substantive_message_count=substantive_message_count,
            objection_response_count=objection_response_count,
            add_notable_flag=self._notable_flag_for_policy_result(policy_result),
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
            await session_store.update_contact_draft(
                session.session_id,
                metadata=self._lead_context_metadata(session, policy_result.service_id),
            )
            if prior_unresolved_metadata:
                await session_store.update_contact_draft(session.session_id, metadata=prior_unresolved_metadata)
            return

        if policy_result.action == PolicyAction.CLARIFY and policy_result.safe_context.get("booking_request"):
            await session_store.set_pending_action(session.session_id, PendingAction.BOOKING_CONTACT.value)
            await session_store.update_contact_draft(
                session.session_id,
                metadata=self._lead_context_metadata(session, policy_result.service_id),
            )
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

        symptom_followup_service_id = str(safe_context.get("symptom_followup_service_id") or "").strip()
        if symptom_followup_service_id:
            # Живёт РОВНО 1 шаг (не +5, как остальные фреймы ниже) — узкий, изолированный
            # фрейм специально под §3.2-гейт (policy/__init__.py::_symptom_followup_result):
            # неопределённый ответ на уточняющий вопрос ("где-то полгода") не должен
            # проваливаться в дефолтный "не нашёл". Не расширяет и не трогает
            # service_interest/fact_question ниже — своя ветка в _contextual_frame_classification.
            return ContextFrame(
                frame_type="symptom_followup",
                entity_type="service",
                entity_id=symptom_followup_service_id,
                last_intent=last_intent,
                expires_at_turn=session.message_count + 1,
            )

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

        if not service_id and safe_context.get("question_type") == "cosmetic_article_guidance":
            suggested_services = safe_context.get("suggested_services")
            if isinstance(suggested_services, list) and len(suggested_services) > 1:
                candidates = [
                    {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
                    for item in suggested_services
                    if isinstance(item, dict) and item.get("id")
                ]
                if candidates:
                    return ContextFrame(
                        frame_type="cosmetic_candidates",
                        slots={"candidates": candidates},
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
        if delivery_service is not None:
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

        bridge = getattr(self.request.app.state, "telegram_bridge_service", None)
        if bridge is None or not bridge.enabled:
            return
        try:
            session = await self.request.app.state.session_store.get(session_id)
            if session is not None and session.telegram_topic_id is not None:
                return  # тема уже есть (например, повторный запрос) — не дублируем карточку
            client_label = client_label_for_session(session) if session is not None else session_id[:8]
            await bridge.post_operator_queue_card(
                session_id=session_id,
                reason="⚡️ Запросил оператора",
                last_message=message,
                client_label=client_label,
            )
        except Exception as error:
            logger.warning(
                "telegram_bridge queue card failed session_id=%s error=%s",
                session_id,
                type(error).__name__,
            )

    async def _notify_telegram_for_lead(self, lead, *, reason: str) -> None:
        """Лид с needs_operator=True реально ждёт живого человека — карточка в General
        с клеймом (как operator_requested). Обычный лид/запись — контакт уже зафиксирован,
        бот уже дал полный ответ, никто прямо сейчас не ждёт у монитора — простая карточка
        в тему "Клиенты", без клейма и без своей темы."""

        bridge = getattr(self.request.app.state, "telegram_bridge_service", None)
        if bridge is None or not bridge.enabled:
            return
        try:
            if lead.needs_operator:
                session = await self.request.app.state.session_store.get(lead.session_id)
                if session is not None and session.telegram_topic_id is not None:
                    return
                # contact_draft уже очищен к этому моменту (_clear_contact_state отрабатывает
                # до вызова этого метода) — берём имя/телефон с самого lead, не с сессии.
                client_label = lead.name or lead.phone or f"Сессия {lead.session_id[:8]}"
                await bridge.post_operator_queue_card(
                    session_id=lead.session_id,
                    reason=reason,
                    last_message=lead.summary,
                    client_label=client_label,
                )
            else:
                card_text = (
                    f"{reason}\n\n"
                    f"Имя: {_escape_markdown(lead.name or 'не указано')}\n"
                    f"Телефон: {_escape_markdown(lead.phone or 'не указан')}\n\n"
                    f"{_escape_markdown(lead.summary)}"
                )
                await bridge.post_client_lead_card(card_text)
        except Exception as error:
            logger.warning(
                "telegram_bridge lead notify failed session_id=%s error=%s",
                lead.session_id,
                type(error).__name__,
            )

    async def _forward_to_telegram_topic(self, session, message: str) -> None:
        if not session.telegram_topic_id:
            return
        bridge = getattr(self.request.app.state, "telegram_bridge_service", None)
        if bridge is None or not bridge.enabled:
            return
        try:
            await bridge.forward_client_message(session.session_id, message)
        except Exception as error:
            logger.warning(
                "telegram_bridge forward failed session_id=%s error=%s",
                session.session_id,
                type(error).__name__,
            )

    async def handle_message(
        self,
        *,
        company_id: str,
        session_id: str | None,
        message: str,
    ) -> ChatMessageResponse | JSONResponse:
        """Тонкая обёртка: резолвит/создаёт session_id и сериализует всю обработку одного
        сообщения per-session локом (см. SessionStore.lock_for) — сам pipeline не тронут,
        просто выполняется целиком под локом в _handle_message_locked."""

        session_store = self.request.app.state.session_store
        session = await session_store.get_or_create(session_id, company_id)
        async with session_store.lock_for(session.session_id):
            response = await self._handle_message_locked(
                company_id=company_id,
                session_id=session.session_id,
                message=message,
            )
        await self._track_answer_safe(company_id, session.session_id, message, response)
        return response

    async def _track_answer_safe(
        self,
        company_id: str,
        session_id: str,
        message: str,
        response: ChatMessageResponse | JSONResponse,
    ) -> None:
        if not isinstance(response, ChatMessageResponse):
            return
        analytics_service = self.request.app.state.analytics_service
        session_store = self.request.app.state.session_store
        session = await session_store.get(session_id)
        try:
            await analytics_service.track_answer(
                company_id=company_id,
                session_id=session_id,
                message=message,
                answer=response.answer,
                action=response.action.value,
                policy_reason=session.last_intent if session else None,
            )
        except Exception as error:
            logger.warning(
                "analytics track_answer failed session_id=%s error=%s", session_id, type(error).__name__
            )

    async def _handle_message_locked(
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
            await session_store.set_status(session.session_id, SessionStatus.CLOSED)
            return ChatMessageResponse(
                session_id=session.session_id,
                status=SessionStatus.CLOSED,
                action=PolicyAction.CLARIFY,
                answer=self._phrase("rate_limit", RATE_LIMIT_ANSWER),
                lead_created=False,
                quick_actions=[],
            )

        message = stripped_message[:MAX_MESSAGE_LENGTH]
        session = await session_store.append_message(session.session_id, MessageRole.USER, message) or session
        await self._forward_to_telegram_topic(session, message)

        pending_contact_response = await self._handle_pending_contact(
            session_store=session_store,
            lead_service=lead_service,
            session=session,
            message=message,
            knowledge_base=knowledge_base,
        )
        if pending_contact_response is not None:
            return pending_contact_response

        if (
            session.status == SessionStatus.AI_ACTIVE
            and not session.pending_action
            and self._is_engagement_dismiss_message(message)
        ):
            return await self._engagement_dismiss_response(session_store, session)

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
            waiting_policy_result = await asyncio.to_thread(
                request.app.state.policy_analyzer,
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
                    summary=self._lead_summary(
                        session,
                        message,
                        is_booking_request=False,
                        name=contact.get("name") if isinstance(contact, dict) else None,
                        phone=contact.get("phone") if isinstance(contact, dict) else None,
                    ),
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
                await self._notify_telegram_for_lead(lead, reason="🔔 Новый лид")
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
            policy_result = await asyncio.to_thread(
                request.app.state.policy_analyzer,
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
        policy_result.safe_context["undisclosed_equipment_terms"] = undisclosed_equipment_terms(knowledge_base)

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
                lead_context_start_index = self._lead_context_start_index(session, prior_contact_draft)
                lead_session = self._lead_context_session(session, lead_context_start_index)
                lead = build_lead_from_contact(
                    company_id=session.company_id,
                    session_id=session.session_id,
                    contact=contact,
                    summary=self._lead_summary(
                        lead_session,
                        message,
                        is_booking_request=is_booking_request,
                        name=contact.get("name") if isinstance(contact, dict) else None,
                        phone=contact.get("phone") if isinstance(contact, dict) else None,
                    ),
                    service_id=self._lead_service_id(
                        session,
                        is_booking_request=is_booking_request,
                        policy_service_id=policy_result.service_id,
                        draft=prior_contact_draft,
                    ),
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
                    recent_messages=recent_messages_for(lead_session),
                    operator_url=self._operator_session_url(session.session_id),
                )
                await self._finalize_lead_summary(lead_session, lead)
                await lead_service.save(
                    lead,
                    event_type="booking_created" if is_booking_request else "lead_created",
                )
                lead_created = True
                await session_store.set_lead_requested(session.session_id, True)
                await self._clear_contact_state(session_store, session.session_id)
                await self._notify_telegram_for_lead(
                    lead,
                    reason="📅 Новая запись" if is_booking_request else "🔔 Новый лид",
                )
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
                    referral_service=policy_result.safe_context.get("referral_service"),
                    urgent=policy_result.safe_context.get("escalation_urgency") != "calm",
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
            and policy_result.safe_context.get("article_guidance_candidate")
        ):
            answer = await self._article_guidance_answer(
                message=message,
                safe_context=policy_result.safe_context,
                history=session.messages[-8:],
            )
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

        session = await session_store.get(session.session_id) or session
        answer, response_quick_actions = await self._apply_engagement_offer_if_due(
            session_store=session_store,
            session=session,
            answer=answer,
            quick_actions=response_quick_actions,
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
