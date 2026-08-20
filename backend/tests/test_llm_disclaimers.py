"""проверки enforce_required_disclaimers / _has_price_disclaimer."""

from __future__ import annotations

from app.llm.openai_compatible import _has_price_disclaimer, enforce_required_disclaimers
from app.llm.prompts import PRICE_DISCLAIMER


def test_has_price_disclaimer_recognizes_canonical_phrase() -> None:
    assert _has_price_disclaimer(f"Стоит 5000 руб. {PRICE_DISCLAIMER}", PRICE_DISCLAIMER)


def test_has_price_disclaimer_recognizes_врач_paraphrase() -> None:
    # Живой баг: система намеренно просит модель перефразировать дисклеймер своими словами
    # (BASE_SYSTEM_PROMPT — "без дословного повтора"), и модель ответила "Точную сумму
    # подтвердит врач на консультации" — старый список слов (точнее|уточн|специалист|менеджер)
    # это не ловил, из-за чего enforce_required_disclaimers приклеивал ещё один дисклеймер сверху.
    answer = "Прессотерапия — от 4 500 ₽. Это предварительная стоимость. Точную сумму подтвердит врач на консультации — без неё процедуру не проводим."
    assert _has_price_disclaimer(answer, PRICE_DISCLAIMER)


def test_has_price_disclaimer_rejects_answer_without_disclaimer() -> None:
    assert not _has_price_disclaimer("Прессотерапия — от 4 500 ₽.", PRICE_DISCLAIMER)


def test_enforce_required_disclaimers_does_not_duplicate_врач_paraphrase() -> None:
    answer = "Прессотерапия — от 4 500 ₽. Это предварительная стоимость. Точную сумму подтвердит врач на консультации — без неё процедуру не проводим."
    result = enforce_required_disclaimers(answer, {"question_type": "price"})
    assert result == answer
    assert result.count("предварительн") == 1


def test_enforce_required_disclaimers_appends_when_truly_missing() -> None:
    answer = "Прессотерапия — от 4 500 ₽."
    result = enforce_required_disclaimers(answer, {"question_type": "price"})
    assert PRICE_DISCLAIMER in result
