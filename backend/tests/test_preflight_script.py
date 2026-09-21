"""backend/scripts/preflight.py — запуск preflight одной командой: поиск токена, отчёт, код возврата, пробы."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import preflight as script  # noqa: E402


def test_read_env_file_handles_comments_quotes_and_missing_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# комментарий\nOTHER=1\nOPERATOR_TOKEN='abc-123'\nEMPTY=\n",
        encoding="utf-8",
    )

    assert script._read_env_file(env_file, "OPERATOR_TOKEN") == "abc-123"
    assert script._read_env_file(env_file, "EMPTY") is None
    assert script._read_env_file(env_file, "MISSING") is None
    assert script._read_env_file(tmp_path / "nope.env", "OPERATOR_TOKEN") is None


def test_find_token_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", "from-env")

    assert script.find_token("OPERATOR_TOKEN", "some-container") == "from-env"


@pytest.mark.parametrize(
    ("report_status", "probe_statuses", "expected"),
    [
        ("ok", None, 0),
        ("degraded", None, 1),
        ("error", None, 2),
        ("ok", ["ok", "degraded"], 1),
        ("ok", ["ok", "error"], 2),
        ("skip", None, 0),
    ],
)
def test_exit_code_reflects_worst_status(report_status, probe_statuses, expected) -> None:
    probes = [{"status": status} for status in probe_statuses] if probe_statuses else None

    assert script.overall_exit_code({"status": report_status}, probes) == expected


def test_render_prints_real_report_without_secrets(test_client) -> None:
    report = test_client.get("/api/debug/preflight", headers={"X-Operator-Token": "demo-operator-token"}).json()

    text = script.render(report, None, "http://127.0.0.1:8000")

    assert "Итог:" in text
    assert "rosh_demo" in text
    assert "leads" in text
    assert "demo-operator-token" in text  # значение по умолчанию называется в детали прямо, это не секрет
    assert "Красные линии" not in text


def _fake_trace(reason: str, action: str = "answer") -> dict:
    return {"final_action": action, "steps": [{"step": "policy_decision", "result": {"reason": reason}}]}


def test_probes_mark_critical_mismatch_as_error_and_others_as_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method, url, token, *, body=None, timeout):
        message = body["message"]
        if "жить" in message:  # критичная проба: кризис не распознан
            return _fake_trace("ok")
        if "стоит" in message:  # некритичная: цена ушла в другую причину
            return _fake_trace("unknown_service")
        expected = {
            "болит живот": "regulated_advice",
            "оператора": "operator_requested",
            "запишите": "booking_request",
            "адрес": "ok",
            "хламидиоз": "regulated_advice",
        }
        reason = next(value for key, value in expected.items() if key in message)
        return _fake_trace(reason)

    monkeypatch.setattr(script, "_request", fake_request)

    results = {probe["name"]: probe for probe in script.run_probes("http://x", "t", "rosh_import_demo", 5)}

    assert results["кризис и самоповреждение"]["status"] == "error"
    assert results["цена услуги"]["status"] == "degraded"
    assert results["медицинский вопрос"]["status"] == "ok"
    assert results["контакты"]["status"] == "ok"
    assert len(results) == 7


def test_probe_request_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(script, "_request", broken)

    results = script.run_probes("http://x", "t", "rosh_import_demo", 5)

    assert all(probe["status"] == "error" for probe in results)


def test_save_report_writes_timestamped_file_into_directory(tmp_path: Path) -> None:
    saved = script.save_report(tmp_path / "reports", {"status": "ok"}, None)

    assert saved.parent == tmp_path / "reports"
    assert saved.name.startswith("preflight_") and saved.suffix == ".json"
    assert '"status": "ok"' in saved.read_text(encoding="utf-8")
