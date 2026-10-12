"""карточка «Быстрая запись»: когда бот ждёт номер для новой записи, ответ помечает карточку для
виджета, а день из карточки приходит вместе с номером одним сообщением. Галочка в «Настройках»
выключает карточку — запись идёт как раньше, двумя шагами."""

from __future__ import annotations

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
