"""приложение отказывается стартовать вне dev_mode с токеном оператора по умолчанию.

Раньше это было только предупреждением в /api/debug/preflight — можно было случайно
поднять прод с открытым токеном и не заметить, пока preflight не запустят вручную."""

from __future__ import annotations

import pytest

from app.preflight import DEFAULT_OPERATOR_TOKEN


def _boot(monkeypatch: pytest.MonkeyPatch, managed_env, *, dev_mode: str, operator_token: str):
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("DEV_MODE", dev_mode)
    monkeypatch.setenv("OPERATOR_TOKEN", operator_token)
    get_settings.cache_clear()
    from app.main import app

    return TestClient(app)


def test_refuses_to_start_with_default_token_outside_dev_mode(monkeypatch: pytest.MonkeyPatch, managed_env) -> None:
    client = _boot(monkeypatch, managed_env, dev_mode="false", operator_token=DEFAULT_OPERATOR_TOKEN)

    with pytest.raises(RuntimeError, match="OPERATOR_TOKEN"):
        with client:
            pass


def test_starts_outside_dev_mode_with_a_real_token(monkeypatch: pytest.MonkeyPatch, managed_env) -> None:
    client = _boot(monkeypatch, managed_env, dev_mode="false", operator_token="a-real-operator-token-123")

    with client as opened:
        response = opened.get("/health")

    assert response.status_code in (200, 207)


def test_default_token_is_allowed_in_dev_mode(monkeypatch: pytest.MonkeyPatch, managed_env) -> None:
    client = _boot(monkeypatch, managed_env, dev_mode="true", operator_token=DEFAULT_OPERATOR_TOKEN)

    with client as opened:
        response = opened.get("/health")

    assert response.status_code in (200, 207)
