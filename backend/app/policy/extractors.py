"""Policy extraction helpers."""

from __future__ import annotations

import re
from typing import Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import MessageRole, Session
from .constants import KNOWN_CITY_FORMS, LOCATION_MISMATCH_KEYWORDS, LOCATION_PATTERNS, PHONE_PATTERN


def contains_keyword(normalized_text: str, keywords: set[str]) -> bool:
    return any(keyword in normalized_text for keyword in keywords)


def extract_phone(message: str) -> Optional[str]:
    match = PHONE_PATTERN.search(message)
    if match is None:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    if phone.startswith("8"):
        phone = "+7" + phone[1:]
    return phone


def extract_name(message: str, phone: Optional[str]) -> Optional[str]:
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


def find_unsupported_city(normalized_message: str, company_city: str) -> Optional[str]:
    normalized_company_city = normalize_text(company_city)
    if f"не из {normalized_company_city}" in normalized_message:
        return f"not_{company_city}"

    for city_form, city_name in KNOWN_CITY_FORMS.items():
        if city_form in normalized_message and normalize_text(city_name) != normalized_company_city:
            return city_name
    return None


def is_location_mismatch(message: str, normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
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
