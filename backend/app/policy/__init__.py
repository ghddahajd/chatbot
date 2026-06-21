"""защитный слой политики, который выполняется до любого вызова llm."""

from __future__ import annotations

from typing import Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import PolicyAction, PolicyReason, PolicyResult, Session
from .constants import (
    BOOKING_CONTACT_PROMPT,
    BOOKING_KEYWORDS,
    CONTACT_PROMPT,
    DURATION_KEYWORDS,
    EXPLANATION_KEYWORDS,
    GENERIC_PRICE_MESSAGES,
    HANDOFF_MESSAGE,
    MEDICAL_KEYWORDS,
    NEGATIVE_MESSAGES,
    OPERATOR_SOFT_OFFER_MESSAGE,
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
    has_booking_contact_prompt,
    has_operator_soft_offer,
    is_location_mismatch,
    last_service_from_history,
)
from .intent import classify_and_extract, normalize_classification
from .quick_actions import all_services_context, service_name_quick_actions, services_summary
from .rules import (
    city_prepositional,
    cosmetic_concern_services,
    mentions_unknown_service,
    similar_services_result,
)


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
        intent == "price_question" or contains_keyword(normalized_message, DURATION_KEYWORDS)
    ):
        service = knowledge_base.find_service_by_id(last_service_from_history(session, knowledge_base))
    phone = extract_phone(message)
    operator_requested = contains_keyword(
        normalized_message, set(knowledge_base.company.operator_triggers)
    ) or intent == "operator_request"
    price_requested = intent == "price_question" or contains_keyword(normalized_message, PRICE_KEYWORDS)
    booking_requested = intent == "booking_request" or contains_keyword(normalized_message, BOOKING_KEYWORDS)
    duration_requested = contains_keyword(normalized_message, DURATION_KEYWORDS)
    explanation_requested = contains_keyword(normalized_message, EXPLANATION_KEYWORDS)
    medical_requested = intent == "medical_advice" or contains_keyword(normalized_message, MEDICAL_KEYWORDS)
    unsupported_city = find_unsupported_city(normalized_message, knowledge_base.company.city)
    city_in_text = city_prepositional(knowledge_base.company.city)

    if has_booking_contact_prompt(session) and contains_keyword(normalized_message, NEGATIVE_MESSAGES):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request_cancelled": True,
                "message_to_user": "Ок, заявку на запись не оформляем. Могу подсказать по услугам, ценам или позвать специалиста.",
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    if has_booking_contact_prompt(session) and not phone:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request": True,
                "message_to_user": BOOKING_CONTACT_PROMPT,
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
            safe_context={"company_name": knowledge_base.company.company_name},
        )

    if intent == "off_topic":
        return PolicyResult(
            action=PolicyAction.OFF_TOPIC,
            reason=PolicyReason.OFF_TOPIC,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "message_to_user": (
                    "Это не по моей части — я консультирую по услугам центра. "
                    f"{knowledge_base.company.company_name}. Могу подсказать по услугам или ценам."
                )
            },
            quick_actions=["Посмотреть услуги", "Позвать оператора"],
        )

    if intent == "clarify":
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.7,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": "Не совсем понял. Уточните, пожалуйста, услугу, цену или вопрос для специалиста.",
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

    if medical_requested:
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.MEDICAL_ADVICE,
            service_id=service.id if service else None,
            confidence=0.98,
            safe_context={
                "message_to_user": knowledge_base.company.medical_disclaimer,
                "handoff_message": HANDOFF_MESSAGE,
            },
            quick_actions=["Позвать оператора", "Оставить телефон"],
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
                    "В базе такой услуги не вижу. Могу показать список услуг или передать вопрос специалисту."
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
        if not has_operator_soft_offer(session):
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.OPERATOR_REQUESTED,
                service_id=service.id if service else None,
                confidence=0.9,
                safe_context={"message_to_user": OPERATOR_SOFT_OFFER_MESSAGE},
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
            safe_context={"message_to_user": HANDOFF_MESSAGE},
            quick_actions=["Написать в Telegram", "Открыть сайт"],
        )

    if booking_requested:
        if service is None:
            service = knowledge_base.find_service_by_id(last_service_from_history(session, knowledge_base))
        if service is None and not phone:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.BOOKING_REQUEST,
                confidence=0.86,
                safe_context={
                    "force_direct_answer": True,
                    "booking_request": True,
                    "message_to_user": "На какую услугу хотите оставить заявку на запись?",
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
                "message_to_user": BOOKING_CONTACT_PROMPT,
            },
            quick_actions=["Оставить телефон", "Позвать оператора"],
        )

    if phone and has_booking_contact_prompt(session):
        if service is None:
            service = knowledge_base.find_service_by_id(last_service_from_history(session, knowledge_base))
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
                        "В базе такой услуги не вижу. Могу показать список услуг или передать вопрос специалисту."
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
                    "message_to_user": CONTACT_PROMPT,
                },
            )

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.PRICE_QUESTION,
            service_id=service.id,
            confidence=0.95,
            safe_context={**context, "question_type": "price"},
            quick_actions=["Уточнить цену", "Оставить телефон"],
        )

    if explanation_requested:
        if service is None:
            service = knowledge_base.find_service_by_id(last_service_from_history(session, knowledge_base))
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
                "message_to_user": (
                    f"{service.name} — {service.short_description} "
                    "Детали уточнит специалист."
                )
            },
            quick_actions=["Уточнить цену", "Позвать оператора"],
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
            quick_actions=["Уточнить цену", "Позвать оператора"],
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
            "message_to_user": (
                "Уточните, пожалуйста, что вас интересует? "
                "Могу рассказать про услуги, цены или записать к специалисту."
            )
        },
        quick_actions=["Посмотреть услуги", "Позвать оператора"],
    )
