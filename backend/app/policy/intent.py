"""Local policy intent classification."""

from __future__ import annotations

from typing import Any, Optional

from ..knowledge import _token_prefix_match, normalize_text
from .constants import (
    ALLOWED_CLASSIFIER_INTENTS,
    COSMETIC_CONCERN_KEYWORDS,
    MEDICAL_KEYWORDS,
    OFF_TOPIC_KEYWORDS,
    PRICE_KEYWORDS,
    SERVICE_LIST_FAST_MESSAGES,
    SERVICE_LIST_KEYWORDS,
    SMALL_TALK_KEYWORDS,
    UNKNOWN_SERVICE_KEYWORDS,
)
from .extractors import contains_keyword, is_location_mismatch


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


def normalize_classification(raw_result: dict[str, Any]) -> dict[str, object]:
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
    if is_location_mismatch(message, normalized_message, company_city):
        return {"intent": "location_mismatch", "service_id": None, "confidence": 0.86}
    if contains_keyword(normalized_message, OFF_TOPIC_KEYWORDS):
        return {"intent": "off_topic", "service_id": None, "confidence": 0.82}
    if contains_keyword(normalized_message, UNKNOWN_SERVICE_KEYWORDS):
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.84}

    service_id = _local_service_id(message, known_services)
    if normalized_message in SERVICE_LIST_FAST_MESSAGES or contains_keyword(
        normalized_message, SERVICE_LIST_KEYWORDS
    ):
        return {"intent": "list_services", "service_id": service_id, "confidence": 0.9}
    if contains_keyword(normalized_message, PRICE_KEYWORDS):
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, MEDICAL_KEYWORDS):
        return {"intent": "medical_advice", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, COSMETIC_CONCERN_KEYWORDS):
        return {"intent": "cosmetic_concern", "service_id": service_id, "confidence": 0.82}
    if service_id:
        return {"intent": "service_mention", "service_id": service_id, "confidence": 0.78}
    if contains_keyword(normalized_message, SMALL_TALK_KEYWORDS):
        return {"intent": "small_talk", "service_id": None, "confidence": 0.76}
    return {"intent": "service_mention", "service_id": None, "confidence": 0.0}
