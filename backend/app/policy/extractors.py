"""вспомогательные функции для извлечения данных в политике."""

from __future__ import annotations

import re
from typing import Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import MessageRole, Session
from .constants import (
    BOOKING_CONTACT_PROMPT,
    CONTACT_PROMPT,
    KNOWN_CITY_FORMS,
    LOCATION_MISMATCH_KEYWORDS,
    LOCATION_PATTERNS,
    NEGATIVE_MESSAGES,
    OPERATOR_SOFT_OFFER_MESSAGE,
    PHONE_PATTERN,
)


def contains_keyword(normalized_text: str, keywords: set[str]) -> bool:
    tokens = set(normalized_text.split())
    for keyword in keywords:
        if " " in keyword:
            if keyword in normalized_text:
                return True
            continue
        if len(keyword) <= 3:
            if keyword in tokens:
                return True
            continue
        if keyword in normalized_text:
            return True
    return False


def extract_phone(message: str) -> Optional[str]:
    match = PHONE_PATTERN.search(message)
    if match is None:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    if phone.startswith("8"):
        phone = "+7" + phone[1:]
    return phone


def extract_name(message: str, phone: Optional[str]) -> Optional[str]:
    source = message
    if phone is not None:
        match = PHONE_PATTERN.search(message)
        if match is not None:
            line_start = message.rfind("\n", 0, match.start()) + 1
            line_end = message.find("\n", match.end())
            if line_end == -1:
                line_end = len(message)
            line = message[line_start:line_end]
            local_start = match.start() - line_start
            local_end = match.end() - line_start
            before_phone = line[:local_start].strip()
            after_phone = line[local_end:].strip()
            source = before_phone or after_phone

    cleaned = PHONE_PATTERN.sub(" ", source)
    if phone is not None:
        cleaned = cleaned.replace(phone, " ")
        cleaned = cleaned.replace(phone.removeprefix("+"), " ")
    cleaned = re.sub(r"[\d+\-\(\)]", " ", cleaned)
    cleaned = re.sub(r"[,;:.!?]", " ", cleaned)

    stop_words = {
        "хочу",
        "записаться",
        "запиши",
        "запишите",
        "заявку",
        "заявка",
        "телефон",
        "номер",
        "оставить",
        "оставляю",
        "на",
        "по",
    }
    for word in cleaned.split():
        normalized_word = normalize_text(word)
        if len(normalized_word) < 2 or normalized_word in stop_words:
            continue
        if re.search(r"[A-Za-zА-Яа-яЁё]", normalized_word):
            return normalized_word.title()
    return None


def find_unsupported_city(normalized_message: str, company_city: str) -> Optional[str]:
    normalized_company_city = normalize_text(company_city)
    if f"не из {normalized_company_city}" in normalized_message:
        return f"not_{company_city}"

    for city_form, city_name in KNOWN_CITY_FORMS.items():
        if city_form in normalized_message and normalize_text(city_name) != normalized_company_city:
            return city_name
    return None


def mentions_company_city(normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    company_city_forms = {
        city_form
        for city_form, city_name in KNOWN_CITY_FORMS.items()
        if normalize_text(city_name) == normalized_company_city
    }
    company_city_forms.add(normalized_company_city)

    if contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return False

    for city_form in company_city_forms:
        if f"не из {city_form}" in normalized_message or f"не в {city_form}" in normalized_message:
            return False
        if city_form in normalized_message:
            return True
    return False


def is_location_mismatch(message: str, normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    if mentions_company_city(normalized_message, company_city):
        return False
    if contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return True
    if find_unsupported_city(normalized_message, company_city):
        return True

    for pattern in LOCATION_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        location = normalize_text(match.group(1))
        if location and normalized_company_city not in location:
            return True
    return False


def last_service_from_history(session: Session, knowledge_base: KnowledgeBase) -> Optional[str]:
    previous_messages = session.messages[:-1]
    barrier_keywords = {
        "ботекс",
        "ботокс",
        "ботулинотерапия",
        "филлер",
        "филлеры",
        "контурная пластика",
        "увеличение губ",
        "не из",
        "онлайн",
        "удаленно",
        "удалённо",
        "дистанционно",
        "далеко",
        "покажи услуги",
        "показать услуги",
        "посмотреть услуги",
        "список услуг",
        "прайс",
        "что у вас есть",
    }
    for history_message in reversed(previous_messages[-8:]):
        if history_message.role != MessageRole.USER:
            continue
        normalized_history = normalize_text(history_message.text)
        if contains_keyword(normalized_history, barrier_keywords):
            return None
        service = knowledge_base.search_service(history_message.text)
        if service is not None:
            return service.id
    return None


def has_operator_soft_offer(session: Session) -> bool:
    return any(
        message.role == MessageRole.ASSISTANT and OPERATOR_SOFT_OFFER_MESSAGE in message.text
        for message in session.messages
    )


def has_booking_contact_prompt(session: Session) -> bool:
    for message in reversed(session.messages):
        normalized_message = normalize_text(message.text)
        if message.role == MessageRole.ASSISTANT and "заявку на запись передали" in normalized_message:
            return False
        if message.role == MessageRole.ASSISTANT and "заявку не оформляем" in normalized_message:
            return False
        if message.role == MessageRole.ASSISTANT and BOOKING_CONTACT_PROMPT in message.text:
            return True
        if (
            message.role == MessageRole.ASSISTANT
            and "заявк" in normalized_message
            and "имя" in normalized_message
            and "телефон" in normalized_message
        ):
            return True
    return False


def has_contact_prompt(session: Session) -> bool:
    for message in reversed(session.messages):
        normalized_message = normalize_text(message.text)
        if message.role == MessageRole.ASSISTANT and "передали ваши контакты" in normalized_message:
            return False
        if message.role == MessageRole.ASSISTANT and "контакт не оставляем" in normalized_message:
            return False
        if message.role == MessageRole.ASSISTANT and CONTACT_PROMPT in message.text:
            return True
        if (
            message.role == MessageRole.ASSISTANT
            and "имя" in normalized_message
            and "телефон" in normalized_message
            and "заявк" not in normalized_message
        ):
            return True
    return False
