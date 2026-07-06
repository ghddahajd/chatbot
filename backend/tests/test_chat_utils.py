"""проверки вспомогательной логики chat route."""

import pytest

from app.routes.chat_utils import (
    CONSULTATION_RISK_SAFE,
    classify_consultation_risk,
    should_ignore_model_location_mismatch,
    should_ignore_model_regulated_advice,
)


def test_ignore_model_location_mismatch_when_user_is_in_company_city() -> None:
    assert should_ignore_model_location_mismatch(
        "я из района динамо в москве норм?",
        "Москва",
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "location_mismatch", "service_id": None, "confidence": 0.91},
    )


def test_keep_model_location_mismatch_for_nearby_city() -> None:
    assert not should_ignore_model_location_mismatch(
        "а из химок?",
        "Москва",
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "location_mismatch", "service_id": None, "confidence": 0.91},
    )


def test_ignore_model_regulated_for_non_medical_domain() -> None:
    assert should_ignore_model_regulated_advice(
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.97},
        {
            "type": "auto_service",
            "restricted_advice": ["legal_guarantee", "remote_safety_assessment"],
        },
    )


def test_keep_model_regulated_for_medical_domain() -> None:
    assert not should_ignore_model_regulated_advice(
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.97},
        {"type": "medical", "restricted_advice": ["medical", "diagnosis", "treatment"]},
    )


@pytest.mark.anyio
async def test_consultation_risk_allows_generic_products_question() -> None:
    domain_profile = {"type": "medical", "restricted_advice": ["medical", "diagnosis", "treatment"]}
    context = {"domain_profile": domain_profile, "service": {"name": "Ботулинотерапия"}}

    risk, _request_id = await classify_consultation_risk(
        None,
        "ботулинотерапия какие препараты используете",
        context,
    )

    assert risk == CONSULTATION_RISK_SAFE


@pytest.mark.anyio
async def test_consultation_risk_still_blocks_real_symptom() -> None:
    domain_profile = {"type": "medical", "restricted_advice": ["medical", "diagnosis", "treatment"]}
    context = {"domain_profile": domain_profile, "service": {"name": "Ботулинотерапия"}}

    risk, _request_id = await classify_consultation_risk(
        None,
        "у меня болит после процедуры",
        context,
    )

    assert risk != CONSULTATION_RISK_SAFE
