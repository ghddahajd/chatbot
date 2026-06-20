"""Policy guard executed before any LLM call."""

from __future__ import annotations

import re
from typing import Any, Optional

from .knowledge import KnowledgeBase, _token_prefix_match, normalize_text
from .models import PolicyAction, PolicyReason, PolicyResult, Session


MEDICAL_KEYWORDS = {
    "диагноз",
    "диагностика",
    "лечение",
    "лечить",
    "препарат",
    "таблет",
    "мазь",
    "пить",
    "назначить",
    "антибиотик",
    "болит",
    "боль",
    "температура",
    "акне",
    "прыщ",
    "сыпь",
    "симптом",
}
SMALL_TALK_KEYWORDS = {"привет", "здравствуй", "добрый день", "хай", "ку", "спасибо", "благодарю"}
SERVICE_LIST_FAST_MESSAGES = {"услуги", "список услуг", "прайс"}
OFF_TOPIC_KEYWORDS = {
    "велик",
    "велосипед",
    "цепь",
    "машина",
    "авто",
    "ноутбук",
    "телефон слом",
    "пицца",
    "пиво",
}
UNKNOWN_SERVICE_KEYWORDS = {
    "ботокс",
    "ботулинотерапия",
    "филлер",
    "филлеры",
    "контурная пластика",
    "увеличение губ",
}
PRICE_KEYWORDS = {"цена", "цену", "стоимость", "уточнить цену", "уточнить стоимость", "сколько стоит", "прайс"}
DURATION_KEYWORDS = {"сколько длится", "длительность", "по времени", "сколько времени"}
SERVICE_LIST_KEYWORDS = {
    "какие услуги",
    "покажи услуги",
    "показать услуги",
    "можно услуги",
    "посмотреть услуги",
    "список услуг",
    "список процедур",
    "покажи список услуг",
    "хочу глянуть прайс",
    "хочу услуги",
    "хочу список услуг",
    "услуги есть",
    "что есть",
    "что у вас есть",
}
COSMETIC_CONCERN_KEYWORDS = {
    "жирная кожа",
    "сальная кожа",
    "поры",
    "расширенные поры",
    "черные точки",
    "черные точки",
    "тусклый цвет",
    "тусклая кожа",
    "цвет лица",
    "неровный тон",
}
COSMETIC_CONCERN_SERVICE_MAP = {
    "жирная кожа": ["facial_cleansing", "cosmetologist_consultation"],
    "сальная кожа": ["facial_cleansing", "cosmetologist_consultation"],
    "поры": ["facial_cleansing", "cosmetologist_consultation"],
    "расширенные поры": ["facial_cleansing", "cosmetologist_consultation"],
    "черные точки": ["facial_cleansing", "cosmetologist_consultation"],
    "тусклый цвет": ["biorevitalization", "cosmetologist_consultation"],
    "тусклая кожа": ["biorevitalization", "cosmetologist_consultation"],
    "цвет лица": ["biorevitalization", "cosmetologist_consultation"],
    "неровный тон": ["cosmetologist_consultation", "facial_cleansing"],
}
GENERIC_PRICE_MESSAGES = {
    "хочу уточнить цену",
    "уточнить цену",
    "сколько стоит",
    "сколько стоит?",
    "цена",
    "стоимость",
    "прайс",
}
KNOWN_CITY_FORMS = {
    "москва": "Москва",
    "москве": "Москва",
    "москвы": "Москва",
    "санкт петербург": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "питер": "Санкт-Петербург",
    "питере": "Санкт-Петербург",
    "казань": "Казань",
    "казани": "Казань",
    "нижний новгород": "Нижний Новгород",
    "новосибирск": "Новосибирск",
    "екатеринбург": "Екатеринбург",
    "сочи": "Сочи",
    "краснодар": "Краснодар",
    "ростов": "Ростов-на-Дону",
    "самара": "Самара",
    "уфа": "Уфа",
    "воронеж": "Воронеж",
}
LOCATION_MISMATCH_KEYWORDS = {
    "таджикистан",
    "таджикистана",
    "узбекистан",
    "узбекистана",
    "казахстан",
    "казахстана",
    "кыргызстан",
    "кыргызстана",
    "беларусь",
    "беларуси",
    "армения",
    "армении",
    "грузия",
    "грузии",
    "турция",
    "турции",
    "азербайджан",
    "азербайджана",
}
LOCATION_PATTERNS = (
    re.compile(r"\bя\s+из\s+([а-яa-z\-\s]{3,40})", re.IGNORECASE),
    re.compile(r"\bмы\s+из\s+([а-яa-z\-\s]{3,40})", re.IGNORECASE),
    re.compile(r"\bживу\s+в\s+([а-яa-z\-\s]{3,40})", re.IGNORECASE),
    re.compile(r"\bнахожусь\s+в\s+([а-яa-z\-\s]{3,40})", re.IGNORECASE),
)
HANDOFF_MESSAGE = (
    "Передаю диалог специалисту. Можете дописать детали, оператор увидит историю. "
    "Если хотите, оставьте имя и телефон для обратной связи."
)
CONTACT_PROMPT = "Оставьте имя и телефон, и специалист сможет связаться с вами позже."
PHONE_PATTERN = re.compile(
    r"(?:(?:\+7|8)\s*[\(\-]?\s*\d{3}\s*[\)\-]?\s*\d{3}\s*[\-]?\s*\d{2}\s*[\-]?\s*\d{2})"
)


def _contains_keyword(normalized_text: str, keywords: set[str]) -> bool:
    return any(keyword in normalized_text for keyword in keywords)


ALLOWED_CLASSIFIER_INTENTS = {
    "small_talk",
    "off_topic",
    "list_services",
    "price_question",
    "cosmetic_concern",
    "medical_advice",
    "operator_request",
    "service_mention",
    "unknown_service",
    "location_mismatch",
}


def _known_service_terms(service_payload: dict[str, Any]) -> list[str]:
    terms = [str(service_payload.get("name") or "")]
    synonyms = service_payload.get("synonyms") or []
    if isinstance(synonyms, list):
        terms.extend(str(synonym) for synonym in synonyms)
    return [term for term in terms if term]


def _local_service_id(message: str, known_services: list[dict[str, Any]]) -> Optional[str]:
    query_tokens = [token for token in normalize_text(message).split() if token]
    if not query_tokens:
        return None

    normalized_message = " ".join(query_tokens)
    if "почист" in normalized_message and "лиц" in normalized_message:
        for service_payload in known_services:
            if service_payload.get("id") == "facial_cleansing":
                return "facial_cleansing"

    for service_payload in known_services:
        for term in _known_service_terms(service_payload):
            normalized_term = normalize_text(term)
            if normalized_term and normalized_term in normalized_message:
                return str(service_payload.get("id"))

            term_tokens = [token for token in normalized_term.split() if token]
            if term_tokens and all(
                any(_token_prefix_match(term_token, query_token) for query_token in query_tokens)
                for term_token in term_tokens
            ):
                return str(service_payload.get("id"))

    return None


def _normalize_classification(raw_result: dict[str, Any]) -> dict[str, object]:
    intent = str(raw_result.get("intent") or "service_mention").strip().lower()
    if intent not in ALLOWED_CLASSIFIER_INTENTS:
        intent = "service_mention"

    service_id = raw_result.get("service_id")
    if service_id is not None:
        service_id = str(service_id).strip()
    if not service_id or service_id == "null":
        service_id = None

    try:
        confidence = float(raw_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "intent": intent,
        "service_id": service_id,
        "confidence": min(max(confidence, 0.0), 1.0),
    }


def classify_and_extract(
    message: str,
    known_services: list[dict[str, Any]],
    company_city: str = "Москва",
) -> dict[str, object]:
    """Local fallback when the external classifier is unavailable."""

    normalized_message = normalize_text(message)
    if _is_location_mismatch(message, normalized_message, company_city):
        return {"intent": "location_mismatch", "service_id": None, "confidence": 0.86}
    if _contains_keyword(normalized_message, OFF_TOPIC_KEYWORDS):
        return {"intent": "off_topic", "service_id": None, "confidence": 0.82}
    if _contains_keyword(normalized_message, UNKNOWN_SERVICE_KEYWORDS):
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.84}

    service_id = _local_service_id(message, known_services)
    if normalized_message in SERVICE_LIST_FAST_MESSAGES or _contains_keyword(
        normalized_message, SERVICE_LIST_KEYWORDS
    ):
        return {"intent": "list_services", "service_id": service_id, "confidence": 0.9}
    if _contains_keyword(normalized_message, PRICE_KEYWORDS):
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.86}
    if _contains_keyword(normalized_message, MEDICAL_KEYWORDS):
        return {"intent": "medical_advice", "service_id": service_id, "confidence": 0.86}
    if _contains_keyword(normalized_message, COSMETIC_CONCERN_KEYWORDS):
        return {"intent": "cosmetic_concern", "service_id": service_id, "confidence": 0.82}
    if service_id:
        return {"intent": "service_mention", "service_id": service_id, "confidence": 0.78}
    if _contains_keyword(normalized_message, SMALL_TALK_KEYWORDS):
        return {"intent": "small_talk", "service_id": None, "confidence": 0.76}
    return {"intent": "service_mention", "service_id": None, "confidence": 0.0}


def _extract_phone(message: str) -> Optional[str]:
    match = PHONE_PATTERN.search(message)
    if match is None:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    if phone.startswith("8"):
        phone = "+7" + phone[1:]
    return phone


def _extract_name(message: str, phone: Optional[str]) -> Optional[str]:
    if phone is not None:
        message = message.replace(phone, " ")
    parts = [part.strip() for part in re.split(r"[,;]", message) if part.strip()]
    if not parts:
        return None

    candidate = parts[0]
    words = candidate.split()
    if not words:
        return None
    return words[0].strip().title()


def _find_unsupported_city(normalized_message: str, company_city: str) -> Optional[str]:
    normalized_company_city = normalize_text(company_city)
    if f"не из {normalized_company_city}" in normalized_message:
        return company_city

    for city_form, city_name in KNOWN_CITY_FORMS.items():
        if city_form in normalized_message and normalize_text(city_name) != normalized_company_city:
            return city_name
    return None


def _is_location_mismatch(message: str, normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    if _contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return True
    if _find_unsupported_city(normalized_message, company_city):
        return True

    for pattern in LOCATION_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        location = normalize_text(match.group(1))
        if location and normalized_company_city not in location:
            return True
    return False


def _format_service_list(knowledge_base: KnowledgeBase) -> str:
    services_by_category: dict[str, list[str]] = {}
    for service in knowledge_base.services:
        services_by_category.setdefault(service.category, []).append(service.name)

    lines = ["Сейчас в базе есть такие услуги:"]
    for category, service_names in services_by_category.items():
        lines.append(f"{category}: {', '.join(service_names)}.")
    lines.append("Если нужна цена, напишите название услуги.")
    return "\n".join(lines)


def _all_services_context(knowledge_base: KnowledgeBase) -> list[dict[str, str]]:
    return [
        {
            "name": service.name,
            "category": service.category,
            "short_description": service.short_description,
        }
        for service in knowledge_base.services
    ]


def _service_name_quick_actions(knowledge_base: KnowledgeBase) -> list[dict[str, str]]:
    services = knowledge_base.services[:4] if len(knowledge_base.services) > 5 else knowledge_base.services
    actions = [
        {
            "label": service.name,
            "type": "message",
            "value": service.name,
        }
        for service in services
    ]
    actions.append(
        {
            "label": "Посмотреть все услуги",
            "type": "message",
            "value": "покажи услуги",
        }
    )
    return actions


def _services_summary(services: list[Any]) -> list[dict[str, str]]:
    return [
        {
            "id": service.id,
            "name": service.name,
            "short_description": service.short_description,
        }
        for service in services
    ]


def _similar_services_result(
    message: str,
    knowledge_base: KnowledgeBase,
    confidence: float,
) -> Optional[PolicyResult]:
    similar_services = knowledge_base.find_similar_services(message)
    if not similar_services:
        return None

    service_names = ", ".join(service.name for service in similar_services)
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.SIMILAR_SERVICES_FOUND,
        confidence=confidence,
        safe_context={
            "similar": _services_summary(similar_services),
            "message_to_user": f"Такой услуги в базе не вижу, но есть похожие: {service_names}. Интересно?",
        },
        quick_actions=[
            {"label": service.name, "type": "message", "value": service.name}
            for service in similar_services
        ],
    )


def _cosmetic_concern_services(message: str, knowledge_base: KnowledgeBase) -> list[Any]:
    normalized_message = normalize_text(message)
    service_ids: list[str] = []
    for keyword, mapped_service_ids in COSMETIC_CONCERN_SERVICE_MAP.items():
        if keyword in normalized_message:
            service_ids.extend(mapped_service_ids)

    services = [
        service
        for service_id in service_ids
        if (service := knowledge_base.find_service_by_id(service_id)) is not None
    ]
    if not services:
        services = knowledge_base.find_similar_services(message, threshold=0.45)

    unique_services: list[Any] = []
    seen_ids: set[str] = set()
    for service in services:
        if service.id not in seen_ids:
            unique_services.append(service)
            seen_ids.add(service.id)
    return unique_services[:3]


def _mentions_unknown_service(normalized_message: str) -> bool:
    if normalized_message in GENERIC_PRICE_MESSAGES:
        return False
    service_noise = {
        "сколько",
        "стоит",
        "цена",
        "стоимость",
        "прайс",
        "хочу",
        "уточнить",
        "услуга",
        "услугу",
        "процедура",
        "процедуру",
    }
    tokens = [token for token in normalized_message.split() if token not in service_noise]
    return bool(tokens)


def _last_service_from_history(session: Session, knowledge_base: KnowledgeBase) -> Optional[str]:
    previous_messages = session.messages[:-1]
    for history_message in reversed(previous_messages[-8:]):
        service = knowledge_base.search_service(history_message.text)
        if service is not None:
            return service.id
    return None


def analyze_message(
    message: str,
    session: Session,
    knowledge_base: KnowledgeBase,
    classification: Optional[dict[str, object]] = None,
) -> PolicyResult:
    """Classify the message before any LLM interaction."""

    classification = _normalize_classification(classification or {})
    intent = str(classification["intent"])
    classifier_confidence = float(classification["confidence"])
    normalized_message = normalize_text(message)
    service = knowledge_base.find_service_by_id(classification.get("service_id"))
    if service is None and (
        intent in {"price_question", "service_mention"} or _contains_keyword(normalized_message, DURATION_KEYWORDS)
    ):
        service = knowledge_base.find_service_by_id(_last_service_from_history(session, knowledge_base))
    phone = _extract_phone(message)
    operator_requested = _contains_keyword(
        normalized_message, set(knowledge_base.company.operator_triggers)
    ) or intent == "operator_request"
    price_requested = intent == "price_question" or _contains_keyword(normalized_message, PRICE_KEYWORDS)
    duration_requested = _contains_keyword(normalized_message, DURATION_KEYWORDS)
    medical_requested = intent == "medical_advice" or _contains_keyword(normalized_message, MEDICAL_KEYWORDS)
    unsupported_city = _find_unsupported_city(normalized_message, knowledge_base.company.city)

    if intent == "location_mismatch" or _is_location_mismatch(
        message, normalized_message, knowledge_base.company.city
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.LOCATION_MISMATCH,
            confidence=classifier_confidence or 0.86,
            safe_context={
                "company_city": knowledge_base.company.city,
                "message_to_user": (
                    f"Очный приём только в {knowledge_base.company.city}. "
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

    if intent == "list_services":
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "all_services": _all_services_context(knowledge_base),
                "question_type": "list_services",
            },
            quick_actions=["Уточнить цену", "Позвать оператора"],
        )

    if unsupported_city:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNSUPPORTED_CITY,
            service_id=service.id if service else None,
            confidence=0.82,
            safe_context={
                "city_note": (
                    f"Очный приём только в {knowledge_base.company.city}. "
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
        suggested_services = _cosmetic_concern_services(message, knowledge_base)
        if suggested_services:
            service_names = ", ".join(service.name for service in suggested_services)
            return PolicyResult(
                action=PolicyAction.ANSWER,
                reason=PolicyReason.OK,
                confidence=classifier_confidence or 0.82,
                safe_context={
                    "question_type": "cosmetic_concern",
                    "suggested_services": _services_summary(suggested_services),
                    "message_to_user": (
                        f"Для такого запроса обычно подходят: {service_names}. "
                        "Точные рекомендации даст специалист на консультации."
                    ),
                },
                quick_actions=[
                    {"label": service.name, "type": "message", "value": service.name}
                    for service in suggested_services
                ],
            )

    if intent == "unknown_service":
        similar_result = _similar_services_result(message, knowledge_base, classifier_confidence or 0.78)
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
                        "name": _extract_name(message, phone),
                        "phone": phone,
                    },
                    "service": service.model_dump() if service else None,
                },
            )
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.OPERATOR_REQUESTED,
            service_id=service.id if service else None,
            confidence=0.95,
            safe_context={"message_to_user": HANDOFF_MESSAGE},
            quick_actions=["Написать в Telegram", "Открыть сайт"],
        )

    if phone:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.93,
            safe_context={
                "contact": {
                    "name": _extract_name(message, phone),
                    "phone": phone,
                },
                "service": service.model_dump() if service else None,
            },
        )

    if price_requested:
        if service is None:
            if not _mentions_unknown_service(normalized_message):
                return PolicyResult(
                    action=PolicyAction.CLARIFY,
                    reason=PolicyReason.PRICE_QUESTION_NO_SERVICE,
                    confidence=0.86,
                    safe_context={
                        "message_to_user": "Уточните, пожалуйста, какая услуга вас интересует?",
                        "available_services": [service.name for service in knowledge_base.services],
                    },
                    quick_actions=_service_name_quick_actions(knowledge_base),
                )

            similar_result = _similar_services_result(message, knowledge_base, 0.78)
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
        )

    if session.status.value != "AI_ACTIVE":
        return PolicyResult(
            action=PolicyAction.REJECT,
            reason=PolicyReason.OUT_OF_SCOPE,
            confidence=0.9,
            safe_context={"message_to_user": "Сейчас чат недоступен для AI-ответов."},
        )

    similar_result = _similar_services_result(message, knowledge_base, classifier_confidence or 0.7)
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
