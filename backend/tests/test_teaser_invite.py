"""приглашение у кнопки чата: показы и нажатия доходят до воронки и до команды «воронка»;
цвет кнопки чата из «Настроек» — только hex, иначе основной цвет."""

from __future__ import annotations

from app.ops_bot import OpsBot

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
