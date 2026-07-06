"""проверки adapter между structured classifier и текущей policy."""

from app.llm.classification import normalize_intent_classification
from app.policy.adapter import merge_policy_classifications, structured_to_policy_classification


KNOWN_SERVICES = {"facial_cleansing", "laser_epilation"}


def test_structured_regulated_risk_maps_to_regulated_advice() -> None:
    classification = normalize_intent_classification(
        {
            "intent": "service_mention",
            "risk": "regulated_advice",
            "service_match_type": "none",
            "confidence": 0.97,
            "reason_code": "regulated_risk",
        },
        KNOWN_SERVICES,
    )

    result = structured_to_policy_classification(classification)

    assert result == {
        "intent": "regulated_advice",
        "service_id": None,
        "confidence": 0.97,
    }


def test_structured_ambiguous_service_does_not_pass_service_id() -> None:
    classification = normalize_intent_classification(
        {
            "intent": "service_mention",
            "risk": "safe",
            "service_match_type": "ambiguous",
            "candidate_service_ids": ["facial_cleansing", "laser_epilation"],
            "confidence": 0.66,
            "reason_code": "ambiguous_service",
        },
        KNOWN_SERVICES,
    )

    result = structured_to_policy_classification(classification)

    assert result["intent"] == "service_mention"
    assert result["service_id"] is None


def test_structured_faq_question_passes_to_policy() -> None:
    classification = normalize_intent_classification(
        {
            "intent": "faq_question",
            "risk": "safe",
            "service_match_type": "none",
            "confidence": 0.83,
            "reason_code": "faq_question",
        },
        KNOWN_SERVICES,
    )

    result = structured_to_policy_classification(classification)

    assert result == {
        "intent": "faq_question",
        "service_id": None,
        "confidence": 0.83,
    }


def test_merge_accepts_model_faq_question_from_generic_local_result() -> None:
    result = merge_policy_classifications(
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "faq_question", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "faq_question"


def test_merge_accepts_model_faq_question_without_losing_local_service_id() -> None:
    result = merge_policy_classifications(
        {"intent": "service_mention", "service_id": "laser_epilation", "confidence": 0.72},
        {"intent": "faq_question", "service_id": None, "confidence": 0.82},
    )

    assert result == {
        "intent": "faq_question",
        "service_id": "laser_epilation",
        "confidence": 0.82,
    }


def test_merge_keeps_local_price_over_model_faq_question() -> None:
    result = merge_policy_classifications(
        {"intent": "price_question", "service_id": "laser_epilation", "confidence": 0.86},
        {"intent": "faq_question", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "price_question"
    assert result["service_id"] == "laser_epilation"


def test_merge_keeps_local_faq_question_over_model_service_mention() -> None:
    result = merge_policy_classifications(
        {"intent": "faq_question", "service_id": "laser_epilation", "confidence": 0.84},
        {"intent": "service_mention", "service_id": "laser_epilation", "confidence": 0.91},
    )

    assert result["intent"] == "faq_question"
    assert result["service_id"] == "laser_epilation"


def test_merge_keeps_local_faq_question_over_model_cosmetic_concern() -> None:
    result = merge_policy_classifications(
        {"intent": "faq_question", "service_id": "laser_resurfacing", "confidence": 0.84},
        {"intent": "cosmetic_concern", "service_id": "laser_resurfacing", "confidence": 0.95},
    )

    assert result["intent"] == "faq_question"
    assert result["service_id"] == "laser_resurfacing"


def test_merge_keeps_local_medical_over_model_answer() -> None:
    result = merge_policy_classifications(
        {"intent": "medical_advice", "service_id": None, "confidence": 0.86},
        {"intent": "service_mention", "service_id": "facial_cleansing", "confidence": 0.95},
    )

    assert result["intent"] == "medical_advice"


def test_merge_accepts_model_unknown_service_as_safer_result() -> None:
    result = merge_policy_classifications(
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "unknown_service", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "unknown_service"


def test_merge_accepts_model_unknown_for_price_without_local_service() -> None:
    result = merge_policy_classifications(
        {"intent": "price_question", "service_id": None, "confidence": 0.86},
        {"intent": "unknown_service", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "unknown_service"


def test_merge_keeps_local_service_when_model_loses_it() -> None:
    result = merge_policy_classifications(
        {"intent": "price_question", "service_id": "laser_epilation", "confidence": 0.86},
        {"intent": "price_question", "service_id": None, "confidence": 0.91},
    )

    assert result["service_id"] == "laser_epilation"


def test_merge_keeps_protected_operator_flow() -> None:
    result = merge_policy_classifications(
        {"intent": "operator_request", "service_id": None, "confidence": 0.88},
        {"intent": "small_talk", "service_id": None, "confidence": 0.92},
    )

    assert result["intent"] == "operator_request"


def test_merge_keeps_local_small_talk_when_model_says_off_topic() -> None:
    result = merge_policy_classifications(
        {"intent": "small_talk", "service_id": None, "confidence": 0.76},
        {"intent": "off_topic", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "small_talk"


def test_merge_keeps_local_service_list_when_model_says_off_topic() -> None:
    result = merge_policy_classifications(
        {"intent": "list_services", "service_id": None, "confidence": 0.9},
        {"intent": "off_topic", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "list_services"


def test_merge_keeps_local_operator_when_model_says_off_topic() -> None:
    result = merge_policy_classifications(
        {"intent": "operator_request", "service_id": None, "confidence": 0.88},
        {"intent": "off_topic", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "operator_request"


def test_merge_keeps_local_off_topic_when_model_says_regulated() -> None:
    result = merge_policy_classifications(
        {"intent": "off_topic", "service_id": None, "confidence": 0.82},
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.97},
    )

    assert result["intent"] == "off_topic"


def test_merge_keeps_local_lead_request_when_model_says_off_topic() -> None:
    result = merge_policy_classifications(
        {"intent": "lead_request", "service_id": None, "confidence": 0.88},
        {"intent": "off_topic", "service_id": None, "confidence": 0.91},
    )

    assert result["intent"] == "lead_request"
