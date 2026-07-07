"""защитный слой политики, который выполняется до любого вызова llm."""

from __future__ import annotations

import logging
from typing import Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import PendingAction, PolicyAction, PolicyReason, PolicyResult, Session
from ..services.rag_search import retrieve_article_context
from .constants import (
    BOOKING_KEYWORDS,
    DURATION_KEYWORDS,
    EXPLANATION_KEYWORDS,
    GENERIC_PRICE_MESSAGES,
    LEAD_REQUEST_KEYWORDS,
    NEGATIVE_MESSAGES,
    OPERATOR_REQUEST_KEYWORDS,
    PRICE_KEYWORDS,
    TELEGRAM_KEYWORDS,
    VISIT_KEYWORDS,
    WEBSITE_KEYWORDS,
)
from .extractors import (
    contains_keyword,
    extract_name,
    extract_phone,
    find_unsupported_city,
    is_location_mismatch,
    last_service_from_history,
)
from .intent import classify_and_extract, normalize_classification
from .quick_actions import all_services_context, service_name_quick_actions, services_summary
from .restricted import is_restricted_question
from .rules import (
    city_prepositional,
    cosmetic_concern_services,
    mentions_unknown_service,
    similar_services_result,
)


logger = logging.getLogger(__name__)


def _phrase(knowledge_base: KnowledgeBase, key: str) -> str:
    value = getattr(knowledge_base, "phrasebook", {}).get(key)
    return str(value).strip() if value else ""


def _service_link_action(service) -> dict[str, str] | None:
    page_url = str(getattr(service, "page_url", "") or "").strip()
    if not page_url:
        return None
    return {"label": "Перейти к услуге", "type": "link", "value": page_url}


def _service_quick_actions(service, *labels: str) -> list[object]:
    actions: list[object] = []
    link_action = _service_link_action(service)
    if link_action is not None:
        actions.append(link_action)
    actions.extend(labels)
    return actions


def _article_quick_actions(matches: list[dict[str, object]]) -> list[object]:
    actions: list[object] = []
    top_url = str(matches[0].get("url") or "").strip() if matches else ""
    if top_url:
        actions.append({"label": "Читать статью", "type": "link", "value": top_url})
    actions.append("Позвать оператора")
    return actions


def _retrieve_article_context_safe(message: str) -> list[dict[str, object]]:
    try:
        return retrieve_article_context(message)
    except FileNotFoundError:
        logger.warning("rag article corpus not found; faq_question will clarify")
        return []
    except ValueError as error:
        logger.warning("rag article corpus invalid; faq_question will clarify error=%s", type(error).__name__)
        return []


def _service_variant_examples(service, limit: int = 5) -> list[str]:
    variants = getattr(service, "variants", [])
    if not isinstance(variants, list):
        return []

    examples: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        name = str(variant.get("name") or "").strip()
        price_text = str(variant.get("price_text") or "").strip()
        if not name:
            continue
        examples.append(f"{name} — {price_text}" if price_text else name)
        if len(examples) >= limit:
            break
    return examples


def _service_explanation_message(service) -> str:
    examples = _service_variant_examples(service)
    if examples:
        variants_count = len(getattr(service, "variants", []) or [])
        return (
            f"{service.name} — направление с {variants_count} вариантами в прайсе. "
            "Например: "
            + "; ".join(examples)
            + ". Точный вариант и стоимость лучше подтвердить со специалистом."
        )
    return f"{service.name} — {service.short_description} Детали уточнит специалист."


FACT_VALUE_QUESTION_KEYWORDS = {
    "какие препараты",
    "какой препарат",
    "какие материалы",
    "какой материал",
    "какие филлеры",
    "какой филлер",
    "что используете",
    "чем работаете",
}
HARD_RESTRICTED_KEYWORDS = {
    "диагноз",
    "лечить",
    "что делать",
    "назначить",
    "назначьте",
    "выпишите",
    "таблет",
    "мазь",
    "антибиотик",
    "воспаление",
    "кровит",
    "кровоточ",
    "родинка",
    "опасно",
    "аллергия",
    "зуд",
    "раздражение",
    "беремен",
    "покраснение",
    "припухлость",
    "отек",
    "отёк",
    "инфекция",
    "осложнение",
    "болит",
    "боли",
    "больно",
    "болью",
    "температура",
    "симптом",
    "жжение",
    "жжет",
    "немеет",
    "немеют",
    "онемение",
    "онемел",
    "тошнит",
    "тошнота",
    "головокружение",
    "гной",
    "гное",
    "опух",
    "щиплет",
    "щипет",
}
SAFE_SERVICE_REQUEST_INTENTS = {
    "medical_advice",
    "regulated_advice",
    "price_question",
    "service_mention",
}


def _looks_like_safe_known_service_request(intent: str, normalized_message: str, service) -> bool:
    if service is None or intent not in SAFE_SERVICE_REQUEST_INTENTS:
        return False
    return not contains_keyword(normalized_message, HARD_RESTRICTED_KEYWORDS)


def _fact_guard_result(message: str, knowledge_base: KnowledgeBase) -> PolicyResult | None:
    config = getattr(knowledge_base, "config_payload", {})
    fact_guards = config.get("fact_guards") if isinstance(config, dict) else None
    if not isinstance(fact_guards, list):
        return None

    normalized_message = normalize_text(message)
    for guard in fact_guards:
        if not isinstance(guard, dict):
            continue

        topic = str(guard.get("topic") or "").strip()
        service_id = str(guard.get("service_id") or "").strip() or None
        known_values = [
            str(value).strip()
            for value in guard.get("known_values", [])
            if str(value).strip()
        ]
        blocked_values = [
            str(value).strip()
            for value in guard.get("blocked_values", [])
            if str(value).strip()
        ]
        matched_blocked = [
            value
            for value in blocked_values
            if normalize_text(value) and normalize_text(value) in normalized_message
        ]
        if not matched_blocked:
            continue

        service = knowledge_base.find_service_by_id(service_id)
        allowed_text = ", ".join(known_values) if known_values else "только позиции из базы центра"
        blocked_text = ", ".join(matched_blocked)
        message_to_user = str(guard.get("message_to_user") or "").strip()
        if not message_to_user:
            topic_label = topic or (service.name if service else "услуга")
            message_to_user = (
                f"В базе центра {blocked_text} не указан. "
                f"По теме «{topic_label}» в базе есть: {allowed_text}. "
                "Могу показать страницу услуги или передать вопрос специалисту."
            )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNKNOWN_SERVICE,
            service_id=service.id if service else service_id,
            confidence=0.96,
            safe_context={
                "message_to_user": message_to_user,
                "service": service.model_dump() if service else None,
                "fact_guard": {
                    "topic": topic,
                    "matched_blocked": matched_blocked,
                    "known_values": known_values,
                },
            },
            quick_actions=_service_quick_actions(service, "Позвать оператора", "Посмотреть услуги")
            if service
            else ["Позвать оператора", "Посмотреть услуги"],
        )

    return None


def _fact_guard_known_values_result(
    message: str,
    knowledge_base: KnowledgeBase,
    service,
) -> PolicyResult | None:
    if not contains_keyword(normalize_text(message), FACT_VALUE_QUESTION_KEYWORDS):
        return None

    config = getattr(knowledge_base, "config_payload", {})
    fact_guards = config.get("fact_guards") if isinstance(config, dict) else None
    if not isinstance(fact_guards, list):
        return None

    normalized_message = normalize_text(message)
    for guard in fact_guards:
        if not isinstance(guard, dict):
            continue

        topic = str(guard.get("topic") or "").strip()
        service_id = str(guard.get("service_id") or "").strip() or None
        guard_service = knowledge_base.find_service_by_id(service_id)
        known_values = [
            str(value).strip()
            for value in guard.get("known_values", [])
            if str(value).strip()
        ]
        topic_matches = bool(topic and normalize_text(topic) in normalized_message)
        service_matches = bool(service is not None and service_id and service.id == service_id)
        guard_service_matches = bool(
            guard_service is not None
            and (
                normalize_text(guard_service.name) in normalized_message
                or any(
                    normalized_synonym and normalized_synonym in normalized_message
                    for normalized_synonym in (normalize_text(synonym) for synonym in guard_service.synonyms)
                )
            )
        )
        if not (topic_matches or service_matches or guard_service_matches):
            continue

        selected_service = guard_service or service
        if known_values:
            topic_label = topic or (selected_service.name if selected_service else "этой теме")
            message_to_user = f"По теме «{topic_label}» в базе указаны: {', '.join(known_values)}."
        else:
            topic_label = topic or (selected_service.name if selected_service else "этой теме")
            message_to_user = (
                f"Точный список по теме «{topic_label}» в базе не указан. "
                "Лучше уточнить его у специалиста."
            )

        return PolicyResult(
            action=PolicyAction.ANSWER if known_values else PolicyAction.CLARIFY,
            reason=PolicyReason.OK if known_values else PolicyReason.UNKNOWN_SERVICE,
            service_id=selected_service.id if selected_service else service_id,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "service": selected_service.model_dump() if selected_service else None,
                "fact_guard": {
                    "topic": topic,
                    "known_values": known_values,
                },
            },
            quick_actions=_service_quick_actions(selected_service, "Уточнить цену", "Позвать оператора")
            if selected_service
            else ["Позвать оператора", "Посмотреть услуги"],
        )

    return None


def analyze_message(
    message: str,
    session: Session,
    knowledge_base: KnowledgeBase,
    classification: Optional[dict[str, object]] = None,
) -> PolicyResult:
    """классифицирует сообщение до любого взаимодействия с llm."""

    classification = normalize_classification(classification or {})
    intent = str(classification["intent"])
    classifier_confidence = float(classification["confidence"])
    normalized_message = normalize_text(message)
    service = knowledge_base.find_service_by_id(classification.get("service_id"))
    if intent == "price_question" and normalized_message in GENERIC_PRICE_MESSAGES:
        service = None
    if service is None and (
        intent == "price_question"
        or contains_keyword(normalized_message, DURATION_KEYWORDS)
        or contains_keyword(normalized_message, EXPLANATION_KEYWORDS)
    ):
        service = knowledge_base.find_service_by_id(
            session.last_service_id or last_service_from_history(session, knowledge_base)
        )
    phone = extract_phone(message)
    operator_requested = contains_keyword(
        normalized_message, set(knowledge_base.company.operator_triggers)
    ) or contains_keyword(normalized_message, OPERATOR_REQUEST_KEYWORDS) or intent == "operator_request"
    price_requested = intent == "price_question" or contains_keyword(normalized_message, PRICE_KEYWORDS)
    booking_requested = intent == "booking_request" or contains_keyword(normalized_message, BOOKING_KEYWORDS)
    lead_requested = intent == "lead_request" or contains_keyword(normalized_message, LEAD_REQUEST_KEYWORDS)
    duration_requested = contains_keyword(normalized_message, DURATION_KEYWORDS)
    explanation_requested = contains_keyword(normalized_message, EXPLANATION_KEYWORDS)
    is_restricted, restricted_category = is_restricted_question(message, knowledge_base.domain_profile)
    medical_requested = intent in {"medical_advice", "regulated_advice"} or is_restricted
    if medical_requested and _looks_like_safe_known_service_request(intent, normalized_message, service):
        medical_requested = False
    unsupported_city = find_unsupported_city(normalized_message, knowledge_base.company.city)
    city_in_text = city_prepositional(knowledge_base.company.city)

    if medical_requested:
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.REGULATED_ADVICE,
            service_id=service.id if service else None,
            confidence=0.98,
            safe_context={
                "message_to_user": knowledge_base.company.safety_disclaimer,
                "handoff_message": _phrase(knowledge_base, "handoff_message"),
                "restricted_category": restricted_category,
            },
            quick_actions=["Позвать оператора", "Оставить телефон"],
        )

    if session.pending_action == PendingAction.COLLECT_CONTACT.value and contains_keyword(
        normalized_message, NEGATIVE_MESSAGES
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.CONTACT_PROVIDED,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "contact_request_cancelled": True,
                "message_to_user": _phrase(knowledge_base, "contact_cancelled"),
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    if session.pending_action == PendingAction.BOOKING_CONTACT.value and contains_keyword(
        normalized_message, NEGATIVE_MESSAGES
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request_cancelled": True,
                "message_to_user": _phrase(knowledge_base, "booking_cancelled"),
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    if phone and session.lead_requested:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": (
                    "Контакты уже передали менеджеру. Если нужно изменить заявку, "
                    "допишите детали здесь или позовите оператора."
                ),
            },
            quick_actions=["Позвать оператора", "Посмотреть услуги"],
        )

    if phone and lead_requested:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.94,
            safe_context={
                "contact": {
                    "name": extract_name(message, phone),
                    "phone": phone,
                },
                "service": service.model_dump() if service else None,
            },
        )

    looks_like_new_question = "?" in message or classifier_confidence > 0
    if (
        session.pending_action == PendingAction.BOOKING_CONTACT.value
        and not phone
        and not operator_requested
        and not looks_like_new_question
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request": True,
                "message_to_user": _phrase(knowledge_base, "booking_contact_prompt"),
            },
            quick_actions=["Оставить телефон", "Позвать оператора"],
        )

    if intent == "location_mismatch" or is_location_mismatch(
        message, normalized_message, knowledge_base.company.city
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.LOCATION_MISMATCH,
            confidence=classifier_confidence or 0.86,
            safe_context={
                "company_city": knowledge_base.company.city,
                "message_to_user": (
                    f"Очный приём только в {city_in_text}. "
                    "Уточните, пожалуйста, у специалиста — возможно есть удалённый формат консультации для вашего случая."
                ),
                "context_for_model": {
                    "company_city": knowledge_base.company.city,
                    "note": (
                        f"очный приём только в {knowledge_base.company.city}, "
                        "можно уточнить формат у специалиста"
                    ),
                },
            },
            quick_actions=[
                {
                    "label": "Позвать оператора",
                    "type": "message",
                    "value": "Хочу узнать про удалённый формат",
                }
            ],
        )

    if intent == "small_talk":
        return PolicyResult(
            action=PolicyAction.SMALL_TALK,
            reason=PolicyReason.SMALL_TALK,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "company_name": knowledge_base.company.company_name,
                "phrasebook": getattr(knowledge_base, "phrasebook", {}),
            },
        )

    if intent == "off_topic":
        return PolicyResult(
            action=PolicyAction.OFF_TOPIC,
            reason=PolicyReason.OFF_TOPIC,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "message_to_user": _phrase(knowledge_base, "off_topic")
                or (
                    "Это не по моей части — я консультирую по услугам компании. "
                    f"{knowledge_base.company.company_name}. Могу подсказать по услугам или ценам."
                )
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    fact_guard_result = _fact_guard_result(message, knowledge_base)
    if fact_guard_result is not None:
        return fact_guard_result

    fact_guard_known_values_result = _fact_guard_known_values_result(message, knowledge_base, service)
    if fact_guard_known_values_result is not None:
        return fact_guard_known_values_result

    if intent == "clarify":
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.7,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "clarify")
                or "Не совсем понял. Уточните, пожалуйста, услугу, цену или вопрос для менеджера.",
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    if intent == "list_services":
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "all_services": all_services_context(knowledge_base),
                "question_type": "list_services",
            },
            quick_actions=["Уточнить цену", "Позвать оператора"],
        )

    if intent == "contact_link":
        wants_telegram = contains_keyword(normalized_message, TELEGRAM_KEYWORDS)
        wants_website = contains_keyword(normalized_message, WEBSITE_KEYWORDS)
        wants_visit = contains_keyword(normalized_message, VISIT_KEYWORDS)
        if wants_visit:
            message_to_user = (
                f"Очный приём проходит в {city_in_text}. "
                "Можно уточнить запись и подходящий формат у специалиста."
            )
            quick_actions = ["Позвать оператора", "Открыть сайт"]
        elif wants_telegram and not wants_website:
            message_to_user = "Можно написать нам в Telegram — кнопка ниже."
            quick_actions = ["Написать в Telegram"]
        elif wants_website and not wants_telegram:
            message_to_user = "Сайт центра можно открыть по кнопке ниже."
            quick_actions = ["Открыть сайт"]
        else:
            message_to_user = "Могу дать ссылку на сайт или Telegram, а при необходимости позвать оператора."
            quick_actions = ["Написать в Telegram", "Открыть сайт", "Позвать оператора"]

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.88,
            safe_context={"message_to_user": message_to_user},
            quick_actions=quick_actions,
        )

    if lead_requested:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=classifier_confidence or 0.88,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "contact_prompt"),
                "service": service.model_dump() if service else None,
            },
            quick_actions=["Позвать оператора", "Посмотреть услуги"],
        )

    if unsupported_city:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNSUPPORTED_CITY,
            service_id=service.id if service else None,
            confidence=0.82,
            safe_context={
                "city_note": (
                    f"Очный приём только в {city_in_text}. "
                    "Можем уточнить формат."
                )
            },
            quick_actions=["Позвать оператора", "Написать в Telegram"],
        )

    if intent == "cosmetic_concern":
        suggested_services = cosmetic_concern_services(message, knowledge_base)
        if suggested_services:
            service_names = ", ".join(service.name for service in suggested_services)
            return PolicyResult(
                action=PolicyAction.ANSWER,
                reason=PolicyReason.OK,
                confidence=classifier_confidence or 0.82,
                safe_context={
                    "question_type": "cosmetic_concern",
                    "suggested_services": services_summary(suggested_services),
                    "domain_profile": knowledge_base.domain_profile,
                    "message_to_user": (
                        f"Для такого запроса обычно подходят: {service_names}. "
                        "Точные рекомендации даст специалист на консультации."
                    ),
                },
                quick_actions=[
                    {"label": service.name, "type": "message", "value": service.name}
                    for service in suggested_services
                ]
                + ["Позвать оператора"],
            )

    if intent == "faq_question" and not price_requested and not duration_requested:
        article_query = f"{service.name} {message}" if service is not None else message
        article_matches = _retrieve_article_context_safe(article_query)
        if not article_matches:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.FAQ_QUESTION,
                confidence=classifier_confidence or 0.7,
                safe_context={
                    "message_to_user": (
                        "Точного ответа по этой теме в базе не нашёл. "
                        "Могу передать вопрос специалисту."
                    )
                },
                quick_actions=["Позвать оператора", "Посмотреть услуги"],
            )

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.FAQ_QUESTION,
            confidence=classifier_confidence or 0.85,
            safe_context={
                "article_context": article_matches,
                "question_type": "faq_question",
                "domain_profile": knowledge_base.domain_profile,
                "phrasebook": getattr(knowledge_base, "phrasebook", {}),
            },
            quick_actions=_article_quick_actions(article_matches),
        )

    if intent == "unknown_service":
        similar_result = similar_services_result(message, knowledge_base, classifier_confidence or 0.78)
        if similar_result is not None:
            return similar_result

        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNKNOWN_SERVICE,
            confidence=classifier_confidence or 0.8,
            safe_context={
                "message_to_user": (
                    _phrase(knowledge_base, "unknown_service")
                    or "В базе такой услуги не нашёл. Могу показать список услуг или передать вопрос менеджеру."
                )
            },
            quick_actions=["Позвать оператора", "Посмотреть услуги"],
        )

    if operator_requested:
        if phone:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.CONTACT_PROVIDED,
                service_id=service.id if service else None,
                confidence=0.95,
                safe_context={
                    "contact": {
                        "name": extract_name(message, phone),
                        "phone": phone,
                    },
                    "service": service.model_dump() if service else None,
                },
            )
        if session.pending_action != PendingAction.OFFERED_OPERATOR.value:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.OPERATOR_REQUESTED,
                service_id=service.id if service else None,
                confidence=0.9,
                safe_context={
                    "message_to_user": _phrase(knowledge_base, "operator_soft_offer")
                },
                quick_actions=[
                    {
                        "label": "Сразу к специалисту",
                        "type": "message",
                        "value": "Да, оператора",
                    },
                    {
                        "label": "Сначала спрошу тут",
                        "type": "message",
                        "value": "Сначала спрошу тут",
                    },
                ],
            )
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.OPERATOR_REQUESTED,
            service_id=service.id if service else None,
            confidence=0.95,
            safe_context={"message_to_user": _phrase(knowledge_base, "handoff_message")},
            quick_actions=["Написать в Telegram", "Открыть сайт"],
        )

    if booking_requested:
        if service is None:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        if service is None and phone and " на " in f" {normalized_message} ":
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.82,
                safe_context={
                    "message_to_user": (
                        _phrase(knowledge_base, "unknown_service")
                        or "В базе такой услуги не нашёл. Могу показать список услуг или передать вопрос менеджеру."
                    )
                },
                quick_actions=["Посмотреть услуги", "Позвать оператора"],
            )
        if service is None and not phone:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.BOOKING_REQUEST,
                confidence=0.86,
                safe_context={
                    "force_direct_answer": True,
                    "booking_request": True,
                    "message_to_user": "На какую услугу хотите оставить заявку?",
                },
                quick_actions=service_name_quick_actions(knowledge_base),
            )
        if phone:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.BOOKING_REQUEST,
                service_id=service.id if service else None,
                confidence=0.94,
                safe_context={
                    "booking_request": True,
                    "contact": {
                        "name": extract_name(message, phone),
                        "phone": phone,
                    },
                    "service": service.model_dump() if service else None,
                },
            )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            service_id=service.id if service else None,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request": True,
                "message_to_user": _phrase(knowledge_base, "booking_contact_prompt"),
            },
            quick_actions=["Оставить телефон", "Позвать оператора"],
        )

    if phone and session.pending_action == PendingAction.BOOKING_CONTACT.value:
        if service is None:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.BOOKING_REQUEST,
            service_id=service.id if service else None,
            confidence=0.94,
            safe_context={
                "booking_request": True,
                "contact": {
                    "name": extract_name(message, phone),
                    "phone": phone,
                },
                "service": service.model_dump() if service else None,
            },
        )

    if phone:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.93,
            safe_context={
                "contact": {
                    "name": extract_name(message, phone),
                    "phone": phone,
                },
                "service": service.model_dump() if service else None,
            },
        )

    if price_requested:
        if service is None:
            if not mentions_unknown_service(normalized_message):
                return PolicyResult(
                    action=PolicyAction.CLARIFY,
                    reason=PolicyReason.PRICE_QUESTION_NO_SERVICE,
                    confidence=0.86,
                    safe_context={
                        "message_to_user": "Уточните, пожалуйста, какая услуга вас интересует?",
                        "available_services": [service.name for service in knowledge_base.services],
                    },
                    quick_actions=service_name_quick_actions(knowledge_base),
                )

            similar_result = similar_services_result(message, knowledge_base, 0.78)
            if similar_result is not None:
                return similar_result

            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.8,
                safe_context={
                    "message_to_user": (
                        _phrase(knowledge_base, "unknown_service")
                        or "В базе такой услуги не нашёл. Могу показать список услуг или передать вопрос менеджеру."
                    )
                },
                quick_actions=["Позвать оператора", "Посмотреть услуги"],
            )

        context = knowledge_base.get_service_context(service)
        if context.get("price") is None:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.PRICE_QUESTION,
                service_id=service.id,
                confidence=0.92,
                safe_context={
                    **context,
                    "message_to_user": _phrase(knowledge_base, "contact_prompt"),
                },
            )

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.PRICE_QUESTION,
            service_id=service.id,
            confidence=0.95,
            safe_context={**context, "question_type": "price"},
            quick_actions=_service_quick_actions(service, "Оставить телефон"),
        )

    if explanation_requested:
        if service is None:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        if service is None:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.SERVICE_EXPLANATION,
                confidence=0.78,
                safe_context={
                    "message_to_user": "Уточните, пожалуйста, по какой услуге рассказать подробнее."
                },
                quick_actions=["Посмотреть услуги", "Позвать оператора"],
            )

        context = knowledge_base.get_service_context(service)
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.SERVICE_EXPLANATION,
            service_id=service.id,
            confidence=0.9,
            safe_context={
                **context,
                "question_type": "explanation",
                "message_to_user": _service_explanation_message(service),
            },
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Позвать оператора"),
        )

    if duration_requested:
        if service is None:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.78,
                safe_context={
                    "message_to_user": "Уточните, пожалуйста, по какой услуге нужен срок или длительность."
                },
                quick_actions=["Позвать оператора", "Посмотреть услуги"],
            )

        context = knowledge_base.get_service_context(service)
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.DURATION_QUESTION,
            service_id=service.id,
            confidence=0.9,
            safe_context={**context, "question_type": "duration"},
        )

    if service is not None:
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            service_id=service.id,
            confidence=0.88,
            safe_context=knowledge_base.get_service_context(service),
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Позвать оператора"),
        )

    if session.status.value != "AI_ACTIVE":
        return PolicyResult(
            action=PolicyAction.REJECT,
            reason=PolicyReason.OUT_OF_SCOPE,
            confidence=0.9,
            safe_context={"message_to_user": "Сейчас чат недоступен для AI-ответов."},
        )

    similar_result = similar_services_result(message, knowledge_base, classifier_confidence or 0.7)
    if similar_result is not None:
        return similar_result

    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.OK,
        confidence=classifier_confidence or 0.65,
        safe_context={
            "message_to_user": _phrase(knowledge_base, "clarify")
            or (
                "Уточните, пожалуйста, что вас интересует? "
                "Могу рассказать про услуги, цены или оформить заявку."
            )
        },
        quick_actions=["Посмотреть услуги", "Позвать оператора"],
    )
