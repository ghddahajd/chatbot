"""GET /api/debug/preflight — одна read-only проверка готовности (до правки и после деплоя).

Проверяем: доступ только по токену, форму отчёта, что нарочно сломанное краснеет, что секреты
не попадают в ответ и что одна упавшая проверка не роняет весь отчёт."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

HEADERS = {"X-Operator-Token": "demo-operator-token"}


def _get(client, path: str = "/api/debug/preflight"):
    return client.get(path, headers=HEADERS)


class _FakeBridge:
    def __init__(self, live: dict | None = None, delay: float = 0.0) -> None:
        self.enabled = True
        self.calls = 0
        self._live = live or {
            "enabled": True,
            "bot_token": {"status": "ok", "detail": "bot username: @test_bot"},
            "operators_group": {"status": "ok", "detail": "доступ есть, status=administrator"},
        }
        self._delay = delay

    async def health_check(self) -> dict:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._live


def _install_bridge(client, monkeypatch: pytest.MonkeyPatch, bridge: _FakeBridge, *, topic: str = "42") -> None:
    settings = client.app.state.settings
    monkeypatch.setattr(client.app.state, "telegram_bridge_service", bridge)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-bot-token")
    monkeypatch.setattr(settings, "telegram_operators_group_id", "-1001")
    monkeypatch.setattr(settings, "telegram_clients_topic_id", topic)


def test_preflight_requires_operator_token(test_client) -> None:
    response = test_client.get("/api/debug/preflight")

    assert response.status_code == 403


def test_preflight_report_shape(test_client) -> None:
    response = _get(test_client)

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {"status", "generated_at", "network_checked", "summary", "checks"}
    assert set(payload["checks"]) >= {
        "app",
        "operator_token",
        "knowledge_base",
        "rag_index",
        "llm_provider",
        "delivery",
        "clients",
        "domains",
        "leads",
        "analytics",
        "storage",
        "sessions",
        "logging",
        "telegram",
    }
    assert all("status" in item and "detail" in item for item in payload["checks"].values())
    # mock LLM + токен по умолчанию + нет Telegram → не «всё зелёное», но и не падение
    assert payload["status"] == "degraded"
    assert sum(payload["summary"].values()) >= len(payload["checks"])


def test_preflight_reports_client_data(test_client) -> None:
    clients = _get(test_client).json()["checks"]["clients"]["clients"]

    rosh = next(item for item in clients if item["company_id"] == "rosh_demo")
    assert rosh["status"] == "ok"
    assert rosh["services"] > 0
    assert rosh["prices"] > 0
    assert isinstance(rosh["domains"], list)


def test_preflight_lead_pipeline_is_ok_on_empty_state(test_client) -> None:
    leads = _get(test_client).json()["checks"]["leads"]

    assert leads["status"] == "ok"
    assert leads["writable"] is True
    assert leads["phone_selftest_failed"] == 0
    assert leads["last_lead_at"] is None


def test_preflight_shows_last_lead_time(test_client) -> None:
    from app.utils.jsonl import append_jsonl

    settings = test_client.app.state.settings
    append_jsonl(
        settings.leads_file,
        {
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None).isoformat(),
            "company_id": "rosh_demo",
            "session_id": "abc",
            "name": "Тест",
            "phone": "+79261234567",
        },
    )

    leads = _get(test_client).json()["checks"]["leads"]

    assert leads["status"] == "ok"
    assert leads["last_lead_company"] == "rosh_demo"
    assert "последний 3 ч назад" in leads["detail"]


def test_preflight_flags_unwritable_leads_file(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight._writable", lambda path: False)

    payload = _get(test_client).json()

    assert payload["checks"]["leads"]["status"] == "error"
    assert "недоступен на запись" in payload["checks"]["leads"]["detail"]
    assert payload["status"] == "error"


def test_preflight_flags_broken_phone_extraction(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight.extract_phone", lambda text: None)

    leads = _get(test_client).json()["checks"]["leads"]

    assert leads["status"] == "error"
    assert leads["phone_selftest_failed"] == 3


def test_preflight_default_token_is_error_outside_dev_mode(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_client.app.state.settings, "dev_mode", False)

    payload = _get(test_client).json()

    assert payload["checks"]["operator_token"]["status"] == "error"
    assert payload["checks"]["operator_token"]["is_default"] is True
    assert payload["status"] == "error"


def test_preflight_does_not_leak_secrets(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "operator_token": "Op-Secret-Value-9f3a7c21",
        "telegram_bot_token": "123456:BOT-SECRET-abcdef",
        "telegram_proxy_url": "http://proxyuser:proxy-pass-777@proxy.example:3128",
        "llm_api_key": "LLM-SECRET-key-5b2e",
    }
    settings = test_client.app.state.settings
    for name, value in secrets.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "telegram_operators_group_id", "-1001")
    monkeypatch.setattr(test_client.app.state, "telegram_bridge_service", _FakeBridge())

    response = test_client.get("/api/debug/preflight", headers={"X-Operator-Token": secrets["operator_token"]})

    assert response.status_code == 200
    for value in secrets.values():
        assert value not in response.text
    telegram = response.json()["checks"]["telegram"]
    assert telegram["config"]["proxy_set"] is True
    assert telegram["config"]["bot_token_set"] is True


def test_preflight_telegram_ok_and_network_flag(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = _FakeBridge()
    _install_bridge(test_client, monkeypatch, bridge)

    with_network = _get(test_client).json()["checks"]["telegram"]
    without_network = _get(test_client, "/api/debug/preflight?network=0").json()["checks"]["telegram"]

    assert with_network["status"] == "ok"
    assert "@test_bot" in with_network["detail"]
    assert with_network["network_checked"] is True
    assert without_network["network_checked"] is False
    assert bridge.calls == 1  # второй запрос (network=0) в Telegram не ходил


def test_preflight_telegram_missing_lead_topic_is_degraded(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_bridge(test_client, monkeypatch, _FakeBridge(), topic="")

    telegram = _get(test_client).json()["checks"]["telegram"]

    assert telegram["status"] == "degraded"
    assert "карточки лидов не отправляются" in telegram["detail"]


def test_preflight_telegram_error_makes_overall_error(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    live = {
        "enabled": True,
        "bot_token": {"status": "ok", "detail": "bot username: @test_bot"},
        "operators_group": {"status": "error", "detail": "бот больше не в группе (status=kicked)"},
    }
    _install_bridge(test_client, monkeypatch, _FakeBridge(live))

    payload = _get(test_client).json()

    assert payload["checks"]["telegram"]["status"] == "error"
    assert "kicked" in payload["checks"]["telegram"]["detail"]
    assert payload["status"] == "error"


def test_preflight_telegram_timeout_is_error(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight.TELEGRAM_CHECK_TIMEOUT_SECONDS", 0.05)
    _install_bridge(test_client, monkeypatch, _FakeBridge(delay=1.0))

    telegram = _get(test_client).json()["checks"]["telegram"]

    assert telegram["status"] == "error"
    assert "не ответил" in telegram["detail"]


def test_preflight_reports_recent_telegram_failures(test_client, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app.utils.jsonl import append_jsonl

    failures_file = tmp_path / "tg_failures.jsonl"
    append_jsonl(
        failures_file,
        {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": "client_lead_card", "error_code": 400},
    )
    monkeypatch.setattr(test_client.app.state.settings, "telegram_bridge_failures_file", failures_file)
    _install_bridge(test_client, monkeypatch, _FakeBridge())

    telegram = _get(test_client).json()["checks"]["telegram"]

    assert telegram["status"] == "degraded"
    assert telegram["failures"]["last_24h"] == 1


def test_preflight_one_broken_check_does_not_break_report(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.preflight.check_storage", _boom)

    payload = _get(test_client).json()

    assert payload["checks"]["storage"]["status"] == "error"
    assert "RuntimeError" in payload["checks"]["storage"]["detail"]
    assert payload["checks"]["leads"]["status"] == "ok"


def test_preflight_large_analytics_file_is_degraded(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight.ANALYTICS_LARGE_MB", 0.00001)
    settings = test_client.app.state.settings
    settings.analytics_file.write_text(json.dumps({"timestamp": "2026-01-01T00:00:00"}) + "\n", encoding="utf-8")

    analytics = _get(test_client).json()["checks"]["analytics"]

    assert analytics["status"] == "degraded"
    assert "БД" in analytics["detail"]


def test_preflight_logging_check_reflects_app_logger_level() -> None:
    from app.preflight import check_logging

    app_logger = logging.getLogger("app")
    previous = app_logger.level
    try:
        app_logger.setLevel(logging.WARNING)
        assert check_logging()["status"] == "degraded"
        app_logger.setLevel(logging.INFO)
        assert check_logging()["status"] == "ok"
    finally:
        app_logger.setLevel(previous)
