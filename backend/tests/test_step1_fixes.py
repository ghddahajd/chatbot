"""починки шага 1: жалоба ночью с часами работы, повтор цены варианта только на голое «а сколько?»,
вариант услуги по другой форме слова («подмышек» → «подмышечные впадины»), но не по другому слову
с тем же началом («подбор» ≠ «подбородок»)."""

from __future__ import annotations

from app import policy
from app.models import ContextFrame, PolicyReason, Session
from app.policy.variants import find_variant_matches
from app.routes.chat_utils import _contextual_frame_classification

from .test_policy import _copy_rosh_import_kb


def _closed_now(knowledge_base) -> None:
    knowledge_base.company.working_hours_schedule = {day: None for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def test_complaint_at_night_says_when_the_admin_answers(policy_session, knowledge_base) -> None:
    _closed_now(knowledge_base)

    result = policy.analyze_message("хочу пожаловаться на администратора", policy_session, knowledge_base, {"intent": "unknown", "confidence": 0.5})

    assert result.reason == PolicyReason.COMPLAINT
    assert knowledge_base.company.working_hours in result.safe_context["message_to_user"]
    assert "в ближайшее время" not in result.safe_context["message_to_user"]


def test_complaint_by_day_keeps_the_usual_text(policy_session, knowledge_base) -> None:
    knowledge_base.company.working_hours_schedule = {}

    result = policy.analyze_message("хочу пожаловаться на администратора", policy_session, knowledge_base, {"intent": "unknown", "confidence": 0.5})

    assert "в ближайшее время" in result.safe_context["message_to_user"]


def _consultations(resolver, managed_env):
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    service = next(item for item in knowledge_base.services if item.name == "Консультации")
    return knowledge_base, service


def _after_variant_price(service) -> Session:
    session = Session(company_id="rosh_import_demo")
    session.active_frame = ContextFrame(
        frame_type="service_interest",
        entity_id=service.id,
        slots={"question_type": "variant_price", "variant": service.variants[0]},
    )
    return session


def test_bare_followup_repeats_the_variant_price(resolver, managed_env) -> None:
    knowledge_base, service = _consultations(resolver, managed_env)

    result = _contextual_frame_classification("а сколько?", _after_variant_price(service), {"intent": "unknown"}, knowledge_base)

    assert result["context_topic"] == "variant_repeat_price"


def test_naming_the_service_again_asks_about_the_whole_service(resolver, managed_env) -> None:
    knowledge_base, service = _consultations(resolver, managed_env)
    local = {"intent": "price_question", "service_id": service.id}

    result = _contextual_frame_classification("сколько стоят консультации", _after_variant_price(service), local, knowledge_base)

    assert result is None or result.get("context_topic") != "variant_repeat_price"


def test_variant_found_by_another_word_form(resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    service = next(item for item in knowledge_base.services if item.name == "Лазерная эпиляция")

    matches = find_variant_matches(service, "лазерная эпиляция подмышек цена")

    assert [variant["name"] for variant in matches] == ["Лазерная эпиляция - подмышечные впадины"]


def test_a_different_word_with_the_same_start_is_not_a_variant(resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    service = next(item for item in knowledge_base.services if item.name == "Фотолечение BBL")

    assert find_variant_matches(service, "помогите с подбором процедуры") == []
