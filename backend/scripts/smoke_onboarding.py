"""smoke-проверка полного onboarding flow без Docker и реальных клиентов."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
COMPANY_ID = "smoke_clinic"

sys.path.insert(0, str(BACKEND_DIR))


class SmokeRunner:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, label: str, expected: str, fn: Callable[[], bool]) -> None:
        self.total += 1
        try:
            ok = fn()
        except Exception as error:  # noqa: BLE001 - smoke должен показать ошибку и продолжить.
            ok = False
            expected = f"{expected} ({type(error).__name__}: {error})"

        marker = "✅" if ok else "❌"
        if ok:
            self.passed += 1
        print(f"{marker} {label:<30} {expected}")


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _configure_env(temp_dir: Path, clients_dir: Path) -> None:
    os.environ.update(
        {
            "LLM_PROVIDER": "mock",
            "DEV_MODE": "true",
            "DEFAULT_COMPANY_ID": COMPANY_ID,
            "CLIENTS_DATA_DIR": str(clients_dir),
            "LEADS_FILE": str(temp_dir / "leads.jsonl"),
            "ANALYTICS_FILE": str(temp_dir / "analytics.jsonl"),
            "DELIVERY_OUTBOX_FILE": str(temp_dir / "delivery_outbox.jsonl"),
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "OPERATOR_TOKEN": "demo-operator-token",
        }
    )


def _run_api_checks(runner: SmokeRunner, temp_dir: Path, clients_dir: Path) -> None:
    _configure_env(temp_dir, clients_dir)

    from app.config import get_settings  # noqa: WPS433

    get_settings.cache_clear()

    from fastapi.testclient import TestClient  # noqa: WPS433
    from app.main import app  # noqa: WPS433

    with TestClient(app) as client:
        bootstrap = client.get(
            f"/api/widget/bootstrap?company_id={COMPANY_ID}",
            headers={"origin": "http://localhost:5500"},
        )
        bootstrap_payload = bootstrap.json()
        runner.check(
            "bootstrap",
            "200 + features",
            lambda: bootstrap.status_code == 200
            and bootstrap_payload.get("company_id") == COMPANY_ID
            and bootstrap_payload.get("features") == {
                "operator": True,
                "lead_capture": True,
                "analytics": False,
            },
        )

        list_payload = client.post(
            "/api/chat/message",
            json={"company_id": COMPANY_ID, "session_id": None, "message": "покажи услуги"},
        ).json()
        runner.check(
            "chat list services",
            "template services",
            lambda: "Первичная консультация" in str(list_payload.get("answer") or "")
            and "Пример процедуры" in str(list_payload.get("answer") or ""),
        )

        price_payload = client.post(
            "/api/chat/message",
            json={
                "company_id": COMPANY_ID,
                "session_id": None,
                "message": "сколько стоит первичная консультация",
            },
        ).json()
        runner.check(
            "chat price",
            "от 2 500 ₽",
            lambda: "от 2 500 ₽" in str(price_payload.get("answer") or "")
            and "Предварительно" in str(price_payload.get("answer") or ""),
        )

        unknown_payload = client.post(
            "/api/chat/message",
            json={"company_id": COMPANY_ID, "session_id": None, "message": "есть ботокс?"},
        ).json()
        runner.check(
            "unknown service",
            "clarify",
            lambda: unknown_payload.get("action") == "clarify"
            and "не вижу" in str(unknown_payload.get("answer") or ""),
        )

        lead_payload = client.post(
            "/api/chat/message",
            json={
                "company_id": COMPANY_ID,
                "session_id": None,
                "message": "Иван, +79991234567, хочу записаться",
            },
        ).json()
        leads = _read_jsonl(temp_dir / "leads.jsonl")
        runner.check(
            "lead company_id",
            COMPANY_ID,
            lambda: lead_payload.get("lead_created") is True
            and bool(leads)
            and leads[-1].get("company_id") == COMPANY_ID,
        )

        analytics = client.get(
            f"/api/analytics/summary?company_id={COMPANY_ID}",
            headers={"x-operator-token": "demo-operator-token"},
        )
        analytics_payload = analytics.json()
        runner.check(
            "analytics summary",
            "200",
            lambda: analytics.status_code == 200 and analytics_payload.get("company_id") == COMPANY_ID,
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="chatbot-onboarding-smoke-") as temp:
        temp_dir = Path(temp)
        draft_root = temp_dir / "drafts"
        clients_dir = temp_dir / "clients"
        draft_dir = draft_root / COMPANY_ID
        target_dir = clients_dir / COMPANY_ID

        runner = SmokeRunner()

        create_result = _run_command(
            [
                "backend/scripts/create_kb_draft.py",
                "--company-id",
                COMPANY_ID,
                "--company-name",
                "Smoke Clinic",
                "--city",
                "Москва",
                "--phone",
                "+7 000 000-00-00",
                "--website-url",
                "http://localhost",
                "--output-root",
                str(draft_root),
                "--force",
            ]
        )
        runner.check(
            "create draft",
            "0",
            lambda: create_result.returncode == 0 and draft_dir.exists(),
        )
        if create_result.returncode != 0:
            print(create_result.stderr)
            print(f"\nИТОГО: {runner.passed}/{runner.total} прошло")
            return 1

        validate_draft = _run_command(["backend/scripts/validate_kb.py", str(draft_dir)])
        runner.check("validate draft", "0", lambda: validate_draft.returncode == 0)

        dry_run = _run_command(
            [
                "backend/scripts/onboard_client.py",
                str(draft_dir),
                "--clients-dir",
                str(clients_dir),
                "--api-base",
                "http://localhost:8000",
                "--dry-run",
            ]
        )
        runner.check(
            "dry-run",
            "no publish",
            lambda: dry_run.returncode == 0
            and "Для публикации запустите без --dry-run" in dry_run.stdout
            and not target_dir.exists(),
        )

        publish = _run_command(
            [
                "backend/scripts/onboard_client.py",
                str(draft_dir),
                "--clients-dir",
                str(clients_dir),
                "--api-base",
                "http://localhost:8000",
            ]
        )
        runner.check(
            "publish",
            "target exists",
            lambda: publish.returncode == 0 and target_dir.exists(),
        )

        validate_published = _run_command(["backend/scripts/validate_kb.py", str(target_dir)])
        runner.check("validate published", "0", lambda: validate_published.returncode == 0)

        _run_api_checks(runner, temp_dir, clients_dir)

        print(f"\nИТОГО: {runner.passed}/{runner.total} прошло")
        return 0 if runner.passed == runner.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
