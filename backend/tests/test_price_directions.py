"""цены по направлениям: под общим списком кнопки «Лицо», «Лазер»…, по кнопке — услуги направления с ценами."""

from __future__ import annotations

from app import policy
from app.models import PolicyReason, Session

OVERVIEW = "Какие услуги у вас есть и сколько стоят?"
LIST_SERVICES = {"intent": "list_services", "confidence": 0.9}
UNKNOWN = {"intent": "unknown", "confidence": 0.5}
DIRECTIONS = """  directions:
    - title: Лицо
      items:
        - facial_cleansing
        - biorevitalization
        - unknown_id
    - title: Лазер
      items:
        - laser_epilation
        - laser_acne_treatment
    - title: Пусто
      items:
        - unknown_id
"""


def _kb(managed_env, resolver, *, directions: bool = True):
    config = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    extra = "prices:\n  featured:\n    - facial_cleansing\n    - biorevitalization\n" + (DIRECTIONS if directions else "")
    config.write_text(config.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return resolver.get("rosh_demo", fallback=False)


def _ask(kb, message: str, classification=UNKNOWN):
    return policy.analyze_message(message, Session(company_id="rosh_demo"), kb, classification)


def _labels(result) -> list[str]:
    return [action["label"] if isinstance(action, dict) else action for action in result.quick_actions]


def test_popular_prices_offer_directions(managed_env, resolver) -> None:
    kb = _kb(managed_env, resolver)

    result = _ask(kb, OVERVIEW, LIST_SERVICES)

    assert result.safe_context["message_to_user"].startswith("Популярные направления и цены:")
    # направление без единой услуги из прайса не показываем; менеджера тут не предлагаем — кнопок и так много
    assert _labels(result) == ["Записаться", "Лицо", "Лазер", "Все цены"]


def test_without_directions_the_list_stays_as_before(managed_env, resolver) -> None:
    kb = _kb(managed_env, resolver, directions=False)

    result = _ask(kb, OVERVIEW, LIST_SERVICES)

    assert _labels(result) == ["Записаться", "Все услуги и цены", "Позвать менеджера"]


def test_direction_button_shows_its_services_with_prices(managed_env, resolver) -> None:
    kb = _kb(managed_env, resolver)

    result = _ask(kb, "Цены: лицо")

    text = result.safe_context["message_to_user"]
    assert result.reason == PolicyReason.PRICE_QUESTION
    assert text.splitlines()[:3] == ["Лицо — цены:", "• Чистка лица — от 4 500 ₽", "• Биоревитализация — от 9 500 ₽"]
    assert text.endswith("Записать вас на консультацию?")
    assert _labels(result) == ["Записаться", "Другое направление"]


def test_other_direction_shows_the_directions_again(managed_env, resolver) -> None:
    kb = _kb(managed_env, resolver)

    result = _ask(kb, "Другое направление")

    assert result.safe_context["message_to_user"] == "Какое направление интересует?"
    assert _labels(result) == ["Лицо", "Лазер", "Все цены"]


def test_unknown_direction_is_not_answered_as_a_direction(managed_env, resolver) -> None:
    kb = _kb(managed_env, resolver)

    result = _ask(kb, "Цены: космос")

    assert not str(result.safe_context.get("message_to_user") or "").startswith("Космос")
