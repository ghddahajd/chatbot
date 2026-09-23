"""созвон с РОШ 2026-09-23: не выпячивать «ИИ» в виджете и подсветить запись.

Над ответами бота — «Ассистент» вместо «AI», кнопка «с ИИ» в шапке скрыта (включается
настройкой ai_badge: show), карточка «Записаться на приём» получает рамку цветом из
booking_highlight_color. widget.js отдаётся с no-cache, чтобы правки виджета доходили сразу.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
WIDGET_JS = BACKEND_DIR.parent / "widget" / "widget.js"


def _bootstrap(test_client) -> dict:
    response = test_client.get("/api/widget/bootstrap?company_id=rosh_demo", headers={"origin": "http://localhost:5500"})
    assert response.status_code == 200
    return response.json()["widget_config"]


def _set_widget_config(managed_env, widget: dict) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    payload["widget"] = widget
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_defaults_do_not_advertise_ai(test_client) -> None:
    config = _bootstrap(test_client)

    assert config["assistant_label"] == "Ассистент"
    assert config["ai_badge"] == ""
    assert config["booking_highlight_color"] == ""


def test_client_can_turn_the_ai_badge_back_on_and_rename_the_label(test_client, managed_env) -> None:
    _set_widget_config(managed_env, {"ai_badge": "show", "assistant_label": "Помощник"})

    config = _bootstrap(test_client)

    assert config["ai_badge"] == "show"
    assert config["assistant_label"] == "Помощник"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#2E9E6B", "#2E9E6B"),
        ("#abc", "#abc"),
        ("green", ""),
        ("#12345", ""),
        ("#2E9E6B;background:url(https://evil.test)", ""),
    ],
)
def test_booking_highlight_color_accepts_only_hex(value: str, expected: str, test_client, managed_env) -> None:
    _set_widget_config(managed_env, {"booking_highlight_color": value})

    assert _bootstrap(test_client)["booking_highlight_color"] == expected


def test_widget_js_is_revalidated_on_every_load(test_client) -> None:
    response = test_client.get("/static/widget.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_widget_source_has_no_hardcoded_ai_label() -> None:
    source = WIDGET_JS.read_text(encoding="utf-8")

    assert '" AI"' not in source
    assert "AI-консультант" not in source
    assert "AI печатает" not in source
    # кнопка «с ИИ» спрятана ещё до загрузки настроек — не мигает у клиентов, где выключена
    assert 'aria-label="Что значит «с ИИ»" style="display:none"' in source


def test_rosh_greeting_and_highlight_in_real_data() -> None:
    config_path = BACKEND_DIR / "data" / "clients" / "rosh_import_demo" / "config.yaml"
    if not config_path.exists():
        pytest.skip("нет локальной копии данных РОШ")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    greetings = payload["phrasebook"]["greeting"]

    assert greetings == ["Здравствуйте! Помогу записаться, узнать стоимость услуги или связаться с администратором."]
    assert payload["widget"]["booking_highlight_color"] == "#2E9E6B"
