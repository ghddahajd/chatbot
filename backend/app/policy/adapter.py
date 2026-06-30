"""адаптер structured classifier под текущий policy contract."""

from __future__ import annotations

from typing import Any

from ..llm.classification import IntentClassification


LEGACY_INTENT_BY_STRUCTURED_INTENT = {
    "regulated_advice": "regulated_advice",
    "faq_question": "service_mention",
}
LEGACY_INTENT_BY_RISK = {
    "regulated_advice": "regulated_advice",
    "emergency": "regulated_advice",
    "off_topic": "off_topic",
    "prompt_injection": "off_topic",
}
REGULATED_INTENTS = {"medical_advice", "regulated_advice"}
PROTECTED_LOCAL_INTENTS = {
    "operator_request",
    "booking_request",
    "contact_link",
    "location_mismatch",
}


def structured_to_policy_classification(
    classification: IntentClassification,
) -> dict[str, object]:
    """приводит новый classifier contract к старому dict для analyze_message()."""

    intent = LEGACY_INTENT_BY_RISK.get(
        classification.risk,
        LEGACY_INTENT_BY_STRUCTURED_INTENT.get(classification.intent, classification.intent),
    )
    service_id = classification.service_id if classification.service_match_type == "exact" else None

    return {
        "intent": intent,
        "service_id": service_id,
        "confidence": classification.confidence,
    }


def merge_policy_classifications(
    local_result: dict[str, Any],
    model_result: dict[str, Any],
) -> dict[str, Any]:
    """выбирает более безопасный результат между локальным и model classifier."""

    local_intent = str(local_result.get("intent") or "")
    model_intent = str(model_result.get("intent") or "")
    local_service_id = local_result.get("service_id")
    model_service_id = model_result.get("service_id")
    local_confidence = float(local_result.get("confidence") or 0.0)
    model_confidence = float(model_result.get("confidence") or 0.0)

    if local_intent in REGULATED_INTENTS:
        return local_result
    if model_intent in REGULATED_INTENTS:
        return model_result
    if local_intent in PROTECTED_LOCAL_INTENTS and local_confidence >= 0.75:
        return local_result
    if local_service_id and not model_service_id:
        return local_result
    if model_intent in {"off_topic", "unknown_service", "location_mismatch"}:
        return model_result
    if local_intent in {"unknown_service", "clarify", "location_mismatch"}:
        return local_result
    if (
        local_intent
        in {
            "list_services",
            "price_question",
            "operator_request",
            "contact_link",
            "off_topic",
            "cosmetic_concern",
            "booking_request",
        }
        and model_intent in {"small_talk", "service_mention"}
        and not model_service_id
    ):
        return local_result
    if model_confidence < 0.5 and local_confidence > model_confidence:
        return local_result
    return model_result
