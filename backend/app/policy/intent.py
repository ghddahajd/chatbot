"""локальная классификация намерений для политики."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Optional

from ..knowledge import _token_prefix_match, normalize_text
from .constants import (
    ALLOWED_CLASSIFIER_INTENTS,
    BOOKING_KEYWORDS,
    BOT_IDENTITY_SIGNAL_KEYWORDS,
    CLARIFY_SHORT_MESSAGES,
    COSMETIC_CONCERN_KEYWORDS,
    CONTACT_LINK_KEYWORDS,
    DURATION_KEYWORDS,
    FAQ_QUESTION_KEYWORDS,
    LEAD_REQUEST_KEYWORDS,
    OFF_TOPIC_KEYWORDS,
    OPERATOR_REQUEST_KEYWORDS,
    PRICE_KEYWORDS,
    PROMPT_INJECTION_KEYWORDS,
    SERVICE_LIST_FAST_MESSAGES,
    SERVICE_LIST_KEYWORDS,
    SMALL_TALK_FUZZY_EXCLUDE,
    SMALL_TALK_KEYWORDS,
    UNKNOWN_SERVICE_KEYWORDS,
    VISIT_KEYWORDS,
)
from .extractors import _distance_at_most_one, contains_keyword, fuzzy_contains, is_location_mismatch, mentions_company_city
from .restricted import is_restricted_question


def _known_service_terms(service_payload: dict[str, Any]) -> list[str]:
    terms = [str(service_payload.get("name") or "")]
    synonyms = service_payload.get("synonyms") or []
    if isinstance(synonyms, list):
        terms.extend(str(synonym) for synonym in synonyms)
    return [term for term in terms if term]


def _service_token_match(term_token: str, query_token: str, *, allow_fuzzy: bool = True) -> bool:
    if _token_prefix_match(term_token, query_token):
        return True
    if not allow_fuzzy:
        return False
    if len(term_token) < 6 or len(query_token) < 6:
        return (
            len(term_token) >= 5
            and len(query_token) >= 5
            and term_token[0] == query_token[0]
            and _distance_at_most_one(term_token, query_token)
        )
    first_letters = {term_token[0], query_token[0]}
    if term_token[0] != query_token[0] and first_letters != {"е", "э"}:
        return False
    return SequenceMatcher(None, term_token, query_token).ratio() >= 0.74


def _local_service_id(
    message: str,
    known_services: list[dict[str, Any]],
    *,
    allow_fuzzy: bool = True,
) -> Optional[str]:
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
                any(
                    _service_token_match(term_token, query_token, allow_fuzzy=allow_fuzzy)
                    for query_token in query_tokens
                )
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

    normalized = {
        "intent": intent,
        "service_id": service_id,
        "confidence": min(max(confidence, 0.0), 1.0),
    }
    context_topic = str(raw_result.get("context_topic") or "").strip()
    if context_topic:
        normalized["context_topic"] = context_topic
    context_variant = raw_result.get("context_variant")
    if isinstance(context_variant, dict):
        normalized["context_variant"] = context_variant
    return normalized


def classify_and_extract(
    message: str,
    known_services: list[dict[str, Any]],
    company_city: str = "Москва",
    domain_profile: Any | None = None,
) -> dict[str, object]:
    """локальный резервный путь, если внешний классификатор недоступен."""

    normalized_message = normalize_text(message)
    if normalized_message in CLARIFY_SHORT_MESSAGES:
        return {"intent": "clarify", "service_id": None, "confidence": 0.7}
    if is_location_mismatch(message, normalized_message, company_city):
        return {"intent": "location_mismatch", "service_id": None, "confidence": 0.86}
    has_off_topic_keyword = contains_keyword(normalized_message, OFF_TOPIC_KEYWORDS)
    service_id = _local_service_id(message, known_services, allow_fuzzy=not has_off_topic_keyword)
    if has_off_topic_keyword and service_id is None:
        return {"intent": "off_topic", "service_id": None, "confidence": 0.82}
    if mentions_company_city(normalized_message, company_city):
        return {"intent": "clarify", "service_id": None, "confidence": 0.78}
    if contains_keyword(normalized_message, PROMPT_INJECTION_KEYWORDS):
        return {"intent": "off_topic", "service_id": None, "confidence": 0.96}
    if contains_keyword(normalized_message, OPERATOR_REQUEST_KEYWORDS) and not contains_keyword(
        normalized_message, BOT_IDENTITY_SIGNAL_KEYWORDS
    ):
        return {"intent": "operator_request", "service_id": None, "confidence": 0.9}

    if (
        contains_keyword(normalized_message, FAQ_QUESTION_KEYWORDS)
        and not fuzzy_contains(normalized_message, PRICE_KEYWORDS)
        and not contains_keyword(normalized_message, DURATION_KEYWORDS)
    ):
        return {"intent": "faq_question", "service_id": service_id, "confidence": 0.84}
    if contains_keyword(normalized_message, UNKNOWN_SERVICE_KEYWORDS) and service_id is None:
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.84}
    if service_id is None:
        service_id = _local_service_id(message, known_services)
    if normalized_message in SERVICE_LIST_FAST_MESSAGES or contains_keyword(
        normalized_message, SERVICE_LIST_KEYWORDS
    ):
        return {"intent": "list_services", "service_id": service_id, "confidence": 0.9}
    if contains_keyword(normalized_message, CONTACT_LINK_KEYWORDS) or contains_keyword(
        normalized_message, VISIT_KEYWORDS
    ):
        return {"intent": "contact_link", "service_id": None, "confidence": 0.88}
    if contains_keyword(normalized_message, LEAD_REQUEST_KEYWORDS):
        return {"intent": "lead_request", "service_id": service_id, "confidence": 0.88}
    if fuzzy_contains(normalized_message, PRICE_KEYWORDS):
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, BOOKING_KEYWORDS):
        return {"intent": "booking_request", "service_id": service_id, "confidence": 0.88}
    if (
        service_id is None
        and "делаете" in normalized_message.split()
        and not normalized_message.startswith(("что ", "чем "))
    ):
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.82}
    is_restricted, _category = is_restricted_question(message, domain_profile)
    if is_restricted:
        return {"intent": "medical_advice", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, COSMETIC_CONCERN_KEYWORDS):
        return {"intent": "cosmetic_concern", "service_id": service_id, "confidence": 0.82}
    if service_id:
        return {"intent": "service_mention", "service_id": service_id, "confidence": 0.78}
    if (
        len(normalized_message.split()) <= 2
        and normalized_message not in SMALL_TALK_FUZZY_EXCLUDE
        and fuzzy_contains(normalized_message, SMALL_TALK_KEYWORDS)
    ):
        return {"intent": "small_talk", "service_id": None, "confidence": 0.76}
    return {"intent": "service_mention", "service_id": None, "confidence": 0.0}
