"""Response validation and deterministic fallbacks."""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)
_intercept_count = 0

RAW_CONTEXT_PATTERNS = (
    re.compile(r"\bservice_id\b", re.IGNORECASE),
    re.compile(r"\bprice_text\b", re.IGNORECASE),
    re.compile(r"\bsafe_context\b", re.IGNORECASE),
    re.compile(r"\bshort_description\b", re.IGNORECASE),
    re.compile(r"\bquestion_type\b", re.IGNORECASE),
    re.compile(r"\{[^{}]*(?:service|price|company|service_id|price_text)[^{}]*\}", re.IGNORECASE | re.DOTALL),
)


def validate_response(answer: str) -> bool:
    """Return False when the model leaked raw context/JSON-like data."""

    if not answer.strip():
        return False
    return not any(pattern.search(answer) for pattern in RAW_CONTEXT_PATTERNS)


def validator_intercept_count() -> int:
    return _intercept_count


def clean_template_answer(context: dict[str, Any]) -> str:
    """Build a safe plain-language answer from approved context fields."""

    message_to_user = context.get("message_to_user")
    if isinstance(message_to_user, str) and message_to_user.strip():
        return message_to_user.strip()

    suggested_services = context.get("suggested_services")
    if isinstance(suggested_services, list) and suggested_services:
        names = [
            str(service.get("name"))
            for service in suggested_services
            if isinstance(service, dict) and service.get("name")
        ]
        if names:
            return (
                "Для такого запроса обычно подходят: "
                + ", ".join(names)
                + ". Точные рекомендации даст специалист на консультации."
            )

    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else {}
    company = context.get("company") if isinstance(context.get("company"), dict) else {}

    parts: list[str] = []
    if service.get("name"):
        description = service.get("short_description")
        if description:
            parts.append(f"{service['name']} — {description}")
        else:
            parts.append(str(service["name"]))
    if price.get("price_text"):
        parts.append(f"Стоимость: {price['price_text']}. Предварительно так, точнее сообщит специалист.")
    if parts:
        return " ".join(parts)
    return "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или записать к специалисту."


def fallback_after_invalid_response(answer: str, context: dict[str, Any]) -> str:
    """Log validator interception and return a deterministic clean answer."""

    global _intercept_count
    _intercept_count += 1
    logger.warning("response validator intercepted raw context leak count=%s answer=%r", _intercept_count, answer[:300])
    return clean_template_answer(context)
