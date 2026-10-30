"""карточка «Быстрая запись»: когда бот ждёт номер для новой записи, ответ помечает карточку для
виджета, а день из карточки приходит вместе с номером одним сообщением. Галочка в «Настройках»
выключает карточку — запись идёт как раньше, двумя шагами."""

from __future__ import annotations

import pytest

from app.ops_bot import OpsBot
from app.policy.constants import BOOKING_CONTACT_ALTERNATIVES

from .test_booking_tiles import TILES, _chat, _last_lead, _set_open
from .test_ops_bot import _Alerts
from .test_teaser_invite import _event


def _send(test_client, message: str, session_id: str, booking_day: str) -> dict:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": message, "booking_day": booking_day},
    )
    assert response.status_code == 200
    return response.json()


def _quick_booking_off(test_client, monkeypatch) -> None:
    resolver = test_client.app.state.knowledge_base_resolver
    real = resolver.widget_config
    monkeypatch.setattr(resolver, "widget_config", lambda company_id: {**real(company_id), "quick_booking": ""})


def test_booking_start_shows_the_card_with_days(test_client) -> None:
    _set_open(test_client, True)

    payload = _chat(test_client, "хочу записаться")

    assert payload["answer"] == "Когда вам удобно?"
    assert payload["booking_form"]["days"] == TILES
    assert payload["booking_form"]["open_now"] is True
    # шага «оставьте номер» с кнопками связи больше нет — они идут вместе с карточкой
    assert [contact["label"] for contact in payload["booking_form"]["contacts"]] == BOOKING_CONTACT_ALTERNATIVES


def test_card_at_night_says_the_clinic_is_closed(test_client) -> None:
    _set_open(test_client, False)

    payload = _chat(test_client, "хочу записаться")

    assert payload["booking_form"]["open_now"] is False


@pytest.mark.parametrize("message", ["Позвать менеджера", "Хочу поговорить с администратором"])
def test_night_operator_shows_callback_card(test_client, message) -> None:
    _set_open(test_client, False)

    payload = _chat(test_client, message)

    assert payload["status"] == "AI_ACTIVE"
    assert payload["booking_form"]["purpose"] == "callback"
    assert payload["booking_form"]["days"] == []
    assert payload["booking_form"]["open_now"] is False


def test_night_callback_accepts_phone_and_stops_showing_card(test_client, managed_env) -> None:
    _set_open(test_client, False)
    first = _chat(test_client, "Позвать менеджера")
    retry = _chat(test_client, "123", first["session_id"])
    assert retry["booking_form"]["purpose"] == "callback"

    sent = _chat(test_client, "+7 926 123-45-67", first["session_id"])

    assert sent["lead_created"] is True
    assert sent["booking_form"] is None
    assert _last_lead(managed_env)["phone"] == "+79261234567"


def test_night_callback_can_return_to_prices(test_client) -> None:
    _set_open(test_client, False)
    first = _chat(test_client, "Позвать менеджера")

    prices = _chat(test_client, "сколько стоит чистка лица", first["session_id"])

    assert prices["booking_form"] is None
    assert "₽" in prices["answer"]


def test_callback_card_respects_shared_setting(test_client, monkeypatch) -> None:
    _set_open(test_client, False)
    _quick_booking_off(test_client, monkeypatch)

    assert _chat(test_client, "Позвать менеджера")["booking_form"] is None


def test_daytime_operator_does_not_show_callback_card(test_client) -> None:
    _set_open(test_client, True)

    assert _chat(test_client, "Позвать менеджера")["booking_form"] is None


def test_night_operator_button_opens_callback_card(test_client) -> None:
    _set_open(test_client, False)
    first = _chat(test_client, "сколько стоит чистка лица")
    # ночью подпись кнопки меняется, но команда вызова администратора сохраняется
    button = next(action for action in first["quick_actions"] if action["label"] == "Оставить телефон")

    assert _chat(test_client, button["value"], first["session_id"])["booking_form"]["purpose"] == "callback"


@pytest.mark.parametrize("message", ["не хочу жить", "после укола задыхаюсь", "хочу пожаловаться на врача", "нет, не хочу"])
def test_night_callback_card_does_not_override_safety_or_cancel(test_client, message) -> None:
    _set_open(test_client, False)
    first = _chat(test_client, "Позвать менеджера")

    assert _chat(test_client, message, first["session_id"])["booking_form"] is None


def test_night_operator_with_phone_creates_lead_without_form(test_client) -> None:
    _set_open(test_client, False)

    payload = _chat(test_client, "Позовите менеджера +7 926 123-45-67")

    assert payload["lead_created"] is True
    assert payload["booking_form"] is None


def test_day_and_phone_in_one_message_make_a_lead_with_the_day(test_client, managed_env) -> None:
    _set_open(test_client, True)
    first = _chat(test_client, "хочу записаться")

    payload = _send(test_client, "+7 926 123-45-67", first["session_id"], "Завтра")

    assert payload["lead_created"] is True
    assert payload["booking_form"] is None
    lead = _last_lead(managed_env)
    assert lead["phone"] == "+79261234567"
    assert lead["preferred_time"] == "завтра"


def test_phone_without_a_day_still_makes_a_lead(test_client, managed_env) -> None:
    _set_open(test_client, True)
    first = _chat(test_client, "хочу записаться")

    payload = _send(test_client, "+7 926 123-45-67", first["session_id"], "")

    assert payload["lead_created"] is True
    assert _last_lead(managed_env)["preferred_time"] == ""


def test_known_day_leaves_only_the_phone_field(test_client) -> None:
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, "Завтра", first["session_id"])

    assert payload["booking_form"]["days"] == []


def test_no_card_outside_booking(test_client) -> None:
    payload = _chat(test_client, "сколько стоит чистка лица")

    assert payload["booking_form"] is None


def test_turned_off_in_settings_the_booking_goes_the_old_way(test_client, monkeypatch) -> None:
    on = _chat(test_client, "хочу записаться")
    _quick_booking_off(test_client, monkeypatch)

    off = _chat(test_client, "хочу записаться")

    assert off["booking_form"] is None
    assert (off["answer"], off["quick_actions"]) == (on["answer"], on["quick_actions"])


def _card_visits(test_client) -> None:
    for visitor in ("visitor-a", "visitor-b"):
        _event(test_client, "impression", visitor)
        _event(test_client, "booking-card-shown", visitor)
    _event(test_client, "booking-card-day", "visitor-a")
    _event(test_client, "booking-card-phone", "visitor-a")
    _event(test_client, "booking-card-phone", "visitor-a")  # та же карточка — тот же человек
    _event(test_client, "booking-card-sent", "visitor-a")


def test_card_steps_reach_the_funnel(test_client) -> None:
    _card_visits(test_client)

    funnel = test_client.app.state.analytics_service.conversion_funnel(company_id="rosh_demo", days=7)

    # по переписке не видно, выбрали ли день и начали ли номер — только по этим шагам
    assert funnel["booking_card"] == {"shown": 2, "day": 1, "phone": 1, "sent": 1}


def test_funnel_command_shows_the_card_line(test_client) -> None:
    _card_visits(test_client)

    text = test_client.portal.call(OpsBot(test_client.app, _Alerts()).reply, "воронка")

    assert "карточка записи: показали 2 · день 1 · начали номер 1 · отправили 1" in text


def test_funnel_signals_do_not_use_up_the_chat_limit(managed_env, monkeypatch) -> None:
    # за одним IP у мобильного оператора много людей: сигналы карточки не должны отнимать отправку номера
    monkeypatch.setenv("CHAT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    headers = {"X-Forwarded-For": "203.0.113.20"}
    with TestClient(app) as client:
        signals = [
            client.post(
                "/api/widget/event",
                json={"company_id": "rosh_demo", "session_id": "", "visitor_id": "visitor-a", "kind": kind, "page": "/"},
                headers=headers,
            ).status_code
            for kind in ("impression", "chat-opened", "booking-card-shown", "booking-card-day", "booking-card-phone")
        ]
        message = client.post(
            "/api/chat/message", json={"company_id": "rosh_demo", "session_id": None, "message": "хочу записаться"}, headers=headers
        )
    get_settings.cache_clear()

    assert signals == [200] * 5
    assert message.status_code == 200
