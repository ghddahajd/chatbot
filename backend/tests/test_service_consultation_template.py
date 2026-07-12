from __future__ import annotations

from app.llm.mock import _service_mention_variants, service_consultation_template
from app.models import Message, MessageRole


def test_service_template_mentions_known_service_with_variants() -> None:
    service = {
        "name": "Лазерная эпиляция",
        "variants": [
            {"name": "Лазерная эпиляция - голени", "price_text": "14 400 ₽"},
            {"name": "Лазерная эпиляция - ноги полностью", "price_text": "21 600 ₽"},
        ],
    }

    answer = service_consultation_template({"service": service}, "лазерная эпиляция")

    assert answer
    assert "Лазерная эпиляция" in answer
    assert "Ботулинотерапия" not in answer
    assert "Ксеомин" not in answer


def test_service_template_without_variants_does_not_invent_variant_names() -> None:
    service = {
        "name": "Консультация косметолога",
        "variants": [],
    }

    answers = _service_mention_variants("Консультация косметолога", service)

    assert answers
    assert all("Консультация косметолога" in answer for answer in answers)
    assert all("голени" not in answer.lower() for answer in answers)
    assert all("Ксеомин" not in answer for answer in answers)


def test_service_template_is_stable_for_same_seed() -> None:
    service = {
        "name": "Ботулинотерапия",
        "variants": [
            {"name": "Ботулинотерапия (Ксеомин) 1 ед.", "price_text": "490 ₽"},
            {"name": "Ботулинотерапия (Миотокс) 1 ед.", "price_text": "450 ₽"},
        ],
    }
    history = [Message(role=MessageRole.USER, text="ботулинотерапия")]

    first = service_consultation_template({"service": service}, "а ботулинотерапия?", history)
    second = service_consultation_template({"service": service}, "а ботулинотерапия?", history)

    assert first == second


def test_service_template_variants_do_not_start_with_understood_phrase() -> None:
    service = {
        "name": "Чистка лица",
        "variants": [
            {"name": "Комбинированная чистка лица"},
            {"name": "Ультразвуковая чистка лица"},
        ],
    }

    answers = _service_mention_variants("Чистка лица", service)

    assert answers
    assert all(not answer.startswith("Понял, вы про") for answer in answers)
    assert all("Могу коротко сориентировать" not in answer for answer in answers)
