"""проверки structured intent schema."""

from app.llm.classification import (
    classification_to_legacy_result,
    normalize_intent_classification,
    safe_normalize_intent_classification,
)


KNOWN_SERVICES = {"facial_cleansing", "laser_epilation", "cosmetologist_consultation"}


def test_exact_service_classification_maps_to_legacy() -> None:
    result = normalize_intent_classification(
        {
            "intent": "price_question",
            "risk": "safe",
            "service_id": "laser_epilation",
            "service_match_type": "exact",
            "confidence": 0.93,
            "reason_code": "price_requested",
        },
        KNOWN_SERVICES,
    )

    assert result.service_id == "laser_epilation"
    assert result.service_match_type == "exact"
    assert classification_to_legacy_result(result) == {
        "intent": "price_question",
        "service_id": "laser_epilation",
        "confidence": 0.93,
    }


def test_unknown_service_cannot_keep_service_id() -> None:
    result = normalize_intent_classification(
        {
            "intent": "unknown_service",
            "risk": "safe",
            "service_id": "botox",
            "service_match_type": "exact",
            "confidence": 0.89,
            "reason_code": "unknown_service",
        },
        KNOWN_SERVICES,
    )

    assert result.service_id is None
    assert result.service_match_type == "unknown"
    assert result.needs_clarification is True
    assert classification_to_legacy_result(result)["service_id"] is None


def test_ambiguous_candidates_are_filtered_to_known_services() -> None:
    result = normalize_intent_classification(
        {
            "intent": "service_mention",
            "risk": "safe",
            "service_id": None,
            "service_match_type": "ambiguous",
            "candidate_service_ids": [
                "facial_cleansing",
                "bad_service",
                "laser_epilation",
                "facial_cleansing",
            ],
            "confidence": 0.62,
            "needs_clarification": False,
            "reason_code": "ambiguous_service",
        },
        KNOWN_SERVICES,
    )

    assert result.service_id is None
    assert result.candidate_service_ids == ["facial_cleansing", "laser_epilation"]
    assert result.needs_clarification is True


def test_regulated_risk_maps_to_legacy_medical_advice() -> None:
    result = normalize_intent_classification(
        {
            "intent": "service_mention",
            "risk": "regulated_advice",
            "service_match_type": "none",
            "confidence": 0.98,
            "reason_code": "regulated_risk",
        },
        KNOWN_SERVICES,
    )

    assert classification_to_legacy_result(result)["intent"] == "medical_advice"


def test_invalid_structured_output_returns_none_for_fallback() -> None:
    result = safe_normalize_intent_classification(
        {
            "intent": "totally_new_intent",
            "risk": "safe",
            "service_match_type": "none",
            "confidence": 0.5,
        },
        KNOWN_SERVICES,
    )

    assert result is None
