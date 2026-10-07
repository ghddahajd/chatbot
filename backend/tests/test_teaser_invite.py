"""приглашение у кнопки чата: показы и нажатия доходят до воронки и до команды «воронка»;
цвет кнопки чата из «Настроек» — только hex, иначе основной цвет."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics import archive_old_analytics_events
from app.ops_bot import OpsBot
from app.utils.jsonl import append_jsonl, read_jsonl

from .test_ops_bot import _Alerts
from .test_settings_routes import _bootstrap_widget, _set_client_widget


def _event(test_client, kind: str, visitor: str) -> None:
    response = test_client.post(
        "/api/widget/event",
        json={"company_id": "rosh_demo", "session_id": "", "visitor_id": visitor, "kind": kind, "page": "/"},
    )
    assert response.status_code == 200


def _teaser_visits(test_client) -> None:
    for visitor in ("visitor-a", "visitor-b"):
        _event(test_client, "impression", visitor)
        _event(test_client, "teaser-shown", visitor)
        _event(test_client, "teaser-shown", visitor)  # повтор той же карточки — тот же человек
    _event(test_client, "teaser-price", "visitor-a")
    _event(test_client, "teaser-booking", "visitor-b")
    _event(test_client, "teaser-booking", "visitor-b")


def test_teaser_shows_and_clicks_reach_the_funnel(test_client) -> None:
    _teaser_visits(test_client)

    funnel = test_client.app.state.analytics_service.conversion_funnel(company_id="rosh_demo", days=7)

    assert funnel["teaser"] == {"shown": 2, "price": 1, "booking": 1}  # уникальные посетители, не клики


def test_funnel_command_shows_the_teaser_line(test_client) -> None:
    _teaser_visits(test_client)

    text = test_client.portal.call(OpsBot(test_client.app, _Alerts()).reply, "воронка")

    assert "приглашение: показали 2 · «Узнать цену» 1 · «Записаться» 1" in text


def test_funnel_command_without_teaser_shows_no_teaser_line(test_client) -> None:
    _event(test_client, "impression", "visitor-a")

    text = test_client.portal.call(OpsBot(test_client.app, _Alerts()).reply, "воронка")

    assert "приглашение" not in text


def test_launcher_color_must_be_hex(test_client, managed_env) -> None:
    # цвет уходит прямо в стиль кнопки на сайте клиента
    _set_client_widget(managed_env, primary_color="#080E0D", button_color="lime; background:url(x)")
    test_client.app.state.knowledge_base_resolver._cache.clear()

    assert _bootstrap_widget(test_client)["button_color"] == "#080E0D"

    _set_client_widget(managed_env, button_color="#ADCE6D")
    test_client.app.state.knowledge_base_resolver._cache.clear()

    assert _bootstrap_widget(test_client)["button_color"] == "#ADCE6D"


def test_old_teaser_shows_are_archived_but_clicks_are_kept(tmp_path) -> None:
    # показ пишется почти на каждую загрузку страницы — без чистки файл аналитики рос бы без конца
    analytics_file, rollup_file = tmp_path / "analytics.jsonl", tmp_path / "rollup.jsonl"
    old = (datetime.utcnow() - timedelta(days=90)).isoformat()
    for event_type in ("teaser_shown", "teaser_price_clicked", "teaser_booking_clicked"):
        append_jsonl(analytics_file, {"timestamp": old, "event_type": event_type, "company_id": "rosh_demo"})

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 1
    assert {entry["event_type"] for entry in read_jsonl(analytics_file)} == {"teaser_price_clicked", "teaser_booking_clicked"}
    assert read_jsonl(rollup_file)[0]["event_type"] == "teaser_shown"
