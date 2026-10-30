"""настройки напоминания доходят до виджета, клики считаются, обратный звонок не становится записью."""

import pytest

from .test_booking_tiles import _chat, _set_open
from .test_settings_routes import _bootstrap_widget, _save_widget
from .test_teaser_invite import _event


def test_reminder_settings_reach_bootstrap_and_can_be_disabled(test_client) -> None:
    response = _save_widget(
        test_client, idle_reminder="on", idle_reminder_seconds="120", idle_reminder_text="Оставить номер для звонка?",
    )
    assert response.status_code == 200
    widget = _bootstrap_widget(test_client)
    assert widget["idle_reminder_seconds"] == "120"
    assert widget["idle_reminder_text"] == "Оставить номер для звонка?"
    assert _save_widget(test_client, idle_reminder="").status_code == 200
    assert _bootstrap_widget(test_client)["idle_reminder"] == ""


@pytest.mark.parametrize("fields", [
    {"idle_reminder": "yes"}, {"idle_reminder_seconds": "0"}, {"idle_reminder_seconds": "3601"},
    {"idle_reminder_seconds": "1.5"}, {"idle_reminder_seconds": "NaN"}, {"idle_reminder_text": ""},
    {"idle_reminder_text": "х" * 301},
])
def test_invalid_reminder_settings_are_rejected(test_client, fields) -> None:
    assert _save_widget(test_client, **fields).status_code == 422


@pytest.mark.parametrize("is_open", [True, False])
def test_callback_button_shows_form_both_day_and_night(test_client, is_open) -> None:
    _set_open(test_client, is_open)
    payload = _chat(test_client, "Перезвоните мне")
    assert payload["status"] == "AI_ACTIVE"
    assert payload["booking_form"]["purpose"] == "callback"
    assert payload["booking_form"]["days"] == []
    assert payload["booking_form"]["open_now"] == is_open


def test_reminder_events_reach_the_funnel_once_per_visitor(test_client) -> None:
    for _ in range(2):
        for kind in ("idle-reminder-shown", "idle-reminder-callback", "idle-reminder-later"):
            _event(test_client, kind, "visitor-a")
    funnel = test_client.app.state.analytics_service.conversion_funnel(company_id="rosh_demo", days=7)
    assert funnel["idle_reminder"] == {"shown": 1, "callback": 1, "later": 1}
