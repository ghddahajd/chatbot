"""проверки domain-aware response validator."""

from app.validator import validate_consultation_response


def test_consultation_validator_blocks_medical_terms_for_medical_profile() -> None:
    context = {"domain_profile": {"type": "medical", "restricted_advice": ["medical_treatment"]}}

    assert not validate_consultation_response("Это безопасно и не опасно после процедуры.", context)


def test_consultation_validator_allows_same_words_for_auto_profile() -> None:
    context = {"domain_profile": {"type": "auto_service", "restricted_advice": ["remote_safety_assessment"]}}

    assert validate_consultation_response("Безопасность автомобиля лучше проверить на диагностике.", context)


def test_consultation_validator_blocks_raw_context_for_any_profile() -> None:
    context = {"domain_profile": {"type": "auto_service", "restricted_advice": []}}

    assert not validate_consultation_response("service_id: oil_change", context)
