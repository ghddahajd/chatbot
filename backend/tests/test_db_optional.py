"""Подготовка БД не должна переключать действующий runtime."""

import subprocess
import sys
from pathlib import Path

from app.sessions import SessionStore


def test_regular_start_keeps_current_stores(test_client):
    state = test_client.app.state
    assert isinstance(state.session_store, SessionStore)
    assert state.lead_service.leads_file is not None
    assert state.delivery_service.outbox_file is not None
    assert state.analytics_service.analytics_file is not None
    assert not hasattr(state, "pg_session_factory")


def test_client_registry_preview_does_not_need_database(managed_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "invalid://must-not-be-used")
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(backend / "scripts/db_admin.py"),
                             "register-clients", "--clients-dir", str(managed_env["clients_dir"]), "--dry-run"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Клиентов к регистрации: 3; БД не изменялась" in result.stdout
