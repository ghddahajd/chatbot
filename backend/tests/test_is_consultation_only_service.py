"""is_consultation_only_service — избегаем "перед консультацией нужна консультация".

2026-08-29: раньше сравнивало category с "консультации" точным совпадением строки — ловит
только буквально эту формулировку. Обобщено до поиска корня "консультац" по category ИЛИ
name, чтобы не ломаться молча для будущего клиента с другой формулировкой категории."""

from app.knowledge import is_consultation_only_service
from app.models import Service


def _service(*, name: str, category: str) -> Service:
    return Service(id="s-1", name=name, category=category, short_description="")


def test_none_service_is_not_consultation() -> None:
    assert is_consultation_only_service(None) is False


def test_exact_category_still_matches_like_before() -> None:
    assert is_consultation_only_service(_service(name="Консультация косметолога", category="Консультации")) is True


def test_differently_worded_category_now_matches() -> None:
    """Живой репро того, что раньше молча ломалось: category != "консультации" буквально."""

    assert is_consultation_only_service(_service(name="Приём врача", category="Консультации врача")) is True
    assert is_consultation_only_service(_service(name="Приём врача", category="Первичная консультация")) is True


def test_consultation_word_in_name_alone_also_matches() -> None:
    assert is_consultation_only_service(_service(name="Первичная консультация", category="Приём")) is True


def test_unrelated_service_is_not_consultation() -> None:
    assert is_consultation_only_service(_service(name="Ботулинотерапия", category="Инъекционная косметология")) is False
