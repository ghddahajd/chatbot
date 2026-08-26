"""проверки /health: структура checks и HTTP-статус (200/207/503)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_health_degraded_when_llm_is_mock(test_client) -> None:
    response = test_client.get("/health")

    assert response.status_code == 207
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["llm_provider"]["status"] == "degraded"
    assert payload["checks"]["llm_provider"]["provider"] == "mock"


def test_health_accepts_head_request(test_client) -> None:
    """Живой баг (UptimeRobot, 2026-08-26): FastAPI не добавляет HEAD в GET-роут
    автоматически — внешние мониторинги (UptimeRobot на бесплатном тарифе, и не только он)
    по умолчанию бьют HEAD, не GET. Без явной регистрации HEAD получали 405 и ложную тревогу
    "сайт упал", хотя GET /health отвечал нормально всё это время."""

    response = test_client.head("/health")

    assert response.status_code in (200, 207)


def test_health_reports_configured_clients(test_client) -> None:
    response = test_client.get("/health")

    payload = response.json()
    kb_check = payload["checks"]["knowledge_base"]
    assert kb_check["status"] == "ok"
    assert kb_check["clients_loaded"] == 3
    assert "rosh_demo" in kb_check["detail"]


def test_health_reports_rag_chunk_count(test_client) -> None:
    response = test_client.get("/health")

    rag_check = response.json()["checks"]["rag_index"]
    assert rag_check["status"] in {"ok", "degraded", "unavailable"}
    assert isinstance(rag_check["chunks_loaded"], int)


def test_health_reports_delivery_dead_events(managed_env: dict[str, Path]) -> None:
    outbox_file = managed_env["temp_dir"] / "delivery_outbox.jsonl"
    outbox_file.parent.mkdir(parents=True, exist_ok=True)
    with outbox_file.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "delivery_id": "d1",
                    "status": "dead",
                    "company_id": "rosh_demo",
                    "event_type": "lead_created",
                }
            )
            + "\n"
        )

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")

    payload = response.json()
    assert payload["checks"]["delivery"]["dead_events"] == 1
    assert payload["checks"]["delivery"]["status"] == "degraded"


def test_health_error_when_no_clients_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_clients_dir = tmp_path / "empty_clients"
    empty_clients_dir.mkdir()
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DEFAULT_COMPANY_ID", "rosh_demo")
    monkeypatch.setenv("CLIENTS_DATA_DIR", str(empty_clients_dir))
    monkeypatch.setenv("LEADS_FILE", str(tmp_path / "leads.jsonl"))
    monkeypatch.setenv("ANALYTICS_FILE", str(tmp_path / "analytics.jsonl"))
    monkeypatch.setenv("DELIVERY_OUTBOX_FILE", str(tmp_path / "delivery_outbox.jsonl"))
    monkeypatch.setenv("DELIVERY_RETRY_ENABLED", "false")
    monkeypatch.setenv("SESSION_EVICTION_ENABLED", "false")
    monkeypatch.setenv("SESSION_SNAPSHOT_FILE", "")
    monkeypatch.setenv("CHAT_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("OPERATOR_TOKEN", "demo-operator-token")

    from app.config import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    # default_company_id="rosh_demo" не существует в пустой clients_dir — резолвер падает
    # на legacy-фолбэк (backend/data/*), поэтому старт приложения сам по себе не падает;
    # именно поэтому /health отдельно перечисляет папки клиентов, а не полагается на то,
    # что приложение вообще поднялось.
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["checks"]["knowledge_base"]["status"] == "error"
    assert payload["checks"]["knowledge_base"]["clients_loaded"] == 0

    get_settings.cache_clear()
