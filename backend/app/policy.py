"""Policy guard executed before any LLM call."""

from __future__ import annotations

import re
from typing import Optional

from .knowledge import KnowledgeBase, normalize_text
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
    "акне",
    "сыпь",
    "симптом",
}
PRICE_KEYWORDS = {"цена", "стоимость", "сколько стоит", "прайс"}
DURATION_KEYWORDS = {"сколько длится", "длительность", "по времени", "сколько времени"}
SERVICE_LIST_KEYWORDS = {
    "какие услуги",
    "список услуг",
    "покажи список услуг",
    "услуги есть",
    "что есть",
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


def _format_service_list(knowledge_base: KnowledgeBase) -> str:
    services_by_category: dict[str, list[str]] = {}
    for service in knowledge_base.services:
        services_by_category.setdefault(service.category, []).append(service.name)

    lines = ["Сейчас в базе есть такие услуги:"]
    for category, service_names in services_by_category.items():
        lines.append(f"{category}: {', '.join(service_names)}.")
    lines.append("Если нужна цена, напишите название услуги.")
    return "\n".join(lines)


def analyze_message(message: str, session: Session, knowledge_base: KnowledgeBase) -> PolicyResult:
    """Classify the message before any LLM interaction."""

    normalized_message = normalize_text(message)
    service = knowledge_base.search_service(message)
    phone = _extract_phone(message)
    operator_requested = _contains_keyword(
        normalized_message, set(knowledge_base.company.operator_triggers)
    )
    price_requested = _contains_keyword(normalized_message, PRICE_KEYWORDS)
    duration_requested = _contains_keyword(normalized_message, DURATION_KEYWORDS)
    medical_requested = _contains_keyword(normalized_message, MEDICAL_KEYWORDS)
    service_list_requested = _contains_keyword(normalized_message, SERVICE_LIST_KEYWORDS)
    unsupported_city = _find_unsupported_city(normalized_message, knowledge_base.company.city)

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

    if service_list_requested:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.9,
            safe_context={"message_to_user": _format_service_list(knowledge_base)},
            quick_actions=["Позвать оператора"],
        )

    if price_requested:
        if service is None:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.8,
                safe_context={
                    "message_to_user": "Уточните, пожалуйста, какая именно услуга вас интересует."
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

    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.UNKNOWN_SERVICE,
        confidence=0.7,
        safe_context={
            "message_to_user": (
                "В базе такой услуги не вижу. Могу показать список услуг или передать вопрос специалисту."
            )
        },
        quick_actions=["Позвать оператора", "Посмотреть услуги"],
    )
