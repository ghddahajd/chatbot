"""проверки вспомогательной логики chat route."""

from types import SimpleNamespace

import pytest

from app.routes.chat_utils import (
    CONSULTATION_RISK_SAFE,
    _contextual_frame_classification,
    _doctor_info_classification,
    classify_consultation_risk,
    should_ignore_model_location_mismatch,
    should_ignore_model_regulated_advice,
)
from app.models import ContextFrame, Session
from app.policy.variants import is_variant_list_question


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


def test_context_frame_resolves_price_followup() -> None:
    session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="service_interest",
            entity_type="service",
            entity_id="consultation",
            entity_label="Консультация",
            expires_at_turn=8,
        ),
    )

    result = _contextual_frame_classification(
        "а всё-таки почём?",
        session,
        {"intent": "clarify", "service_id": None, "confidence": 0.1},
    )

    assert result == {"intent": "price_question", "service_id": "consultation", "confidence": 0.92}


def test_context_frame_resolves_fact_followup() -> None:
    session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="fact_question",
            entity_type="service",
            entity_id="botulinotherapy",
            entity_label="Ботулинотерапия",
            slots={"topic": "botulinotherapy_preparations"},
            expires_at_turn=8,
        ),
    )

    result = _contextual_frame_classification(
        "а какие препараты?",
        session,
        {"intent": "clarify", "service_id": None, "confidence": 0.1},
    )

    assert result == {"intent": "service_mention", "service_id": "botulinotherapy", "confidence": 0.9}


def test_variant_list_question_does_not_match_substring_inside_word() -> None:
    assert not is_variant_list_question("биорезонансная диагностика есть?")
    assert is_variant_list_question("какие зоны есть?")
    assert is_variant_list_question("покажите варианты")
    assert is_variant_list_question("какие позиции?")


def test_context_frame_does_not_treat_bioresonance_as_variant_list() -> None:
    session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="fact_question",
            entity_type="service",
            entity_id="fillers",
            entity_label="Филлеры",
            expires_at_turn=8,
        ),
    )

    result = _contextual_frame_classification(
        "биорезонансная диагностика есть?",
        session,
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
    )

    assert result is None


def test_doctor_info_classification_matches_show_doctors_without_kto() -> None:
    assert _doctor_info_classification("покажи врачей") == {
        "intent": "clinic_info",
        "service_id": None,
        "confidence": 0.88,
        "context_topic": "doctors",
    }
    assert _doctor_info_classification("какие врачи есть") is not None
    assert _doctor_info_classification("покаж врачей") is not None


def test_doctor_info_classification_still_requires_a_trigger_word() -> None:
    assert _doctor_info_classification("врач хороший") is None


def test_context_frame_resolves_doctor_followup_even_if_local_unknown() -> None:
    session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="clinic_info",
            slots={"topic": "doctors"},
            expires_at_turn=8,
        ),
    )

    result = _contextual_frame_classification(
        "а дерматолог кто?",
        session,
        {"intent": "unknown_service", "service_id": None, "confidence": 0.8},
    )

    assert result == {
        "intent": "clinic_info",
        "service_id": None,
        "confidence": 0.9,
        "context_topic": "doctors",
    }


def test_context_frame_resolves_short_doctor_specialty_followup() -> None:
    session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="clinic_info",
            slots={"topic": "doctors"},
            expires_at_turn=8,
        ),
    )

    result = _contextual_frame_classification(
        "а дерматолог?",
        session,
        {"intent": "unknown_service", "service_id": None, "confidence": 0.8},
    )

    assert result == {
        "intent": "clinic_info",
        "service_id": None,
        "confidence": 0.9,
        "context_topic": "doctors",
    }


def test_context_frame_expires_and_does_not_override_explicit_unknown() -> None:
    expired_session = Session(
        company_id="rosh_demo",
        message_count=9,
        active_frame=ContextFrame(
            frame_type="service_interest",
            entity_type="service",
            entity_id="facial_cleaning",
            expires_at_turn=8,
        ),
    )
    active_session = Session(
        company_id="rosh_demo",
        message_count=3,
        active_frame=ContextFrame(
            frame_type="service_interest",
            entity_type="service",
            entity_id="facial_cleaning",
            expires_at_turn=8,
        ),
    )

    assert (
        _contextual_frame_classification(
            "а сколько?",
            expired_session,
            {"intent": "clarify", "service_id": None, "confidence": 0.1},
        )
        is None
    )
    assert (
        _contextual_frame_classification(
            "ботокс",
            active_session,
            {"intent": "unknown_service", "service_id": None, "confidence": 0.8},
        )
        is None
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
async def test_consultation_risk_trusts_policy_cosmetic_concern() -> None:
    class FailingLLM:
        async def classify_restricted_risk(self, message: str) -> str:
            raise AssertionError("policy-approved cosmetic_concern must not call LLM risk check")

    domain_profile = {"type": "medical", "restricted_advice": ["medical", "diagnosis", "treatment"]}
    context = {"domain_profile": domain_profile, "question_type": "cosmetic_concern"}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(llm_client=FailingLLM())))

    risk, _request_id = await classify_consultation_risk(
        request,
        "у меня жирная кожа и расширенные поры, что подойдёт и почём?",
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


@pytest.mark.anyio
async def test_consultation_risk_blocks_worse_after_procedure() -> None:
    domain_profile = {"type": "medical", "restricted_advice": ["medical", "diagnosis", "treatment"]}
    context = {"domain_profile": domain_profile, "service": {"name": "Чистка лица"}}

    risk, _request_id = await classify_consultation_risk(
        None,
        "после процедуры стало хуже",
        context,
    )

    assert risk != CONSULTATION_RISK_SAFE
