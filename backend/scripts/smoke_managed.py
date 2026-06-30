"""smoke-проверка managed-service flow через FastAPI TestClient."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
REQUIRED_KB_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))

from app.utils.jsonl import read_jsonl  # noqa: E402


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
        print(f"{marker} {label:<28} {expected}")


def _replace_company_id(path: Path, company_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^company_id: .*$", f"company_id: {company_id}", text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def _write_duplicate_company(path: Path, company_id: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"company_id: {company_id}",
                f'company_name: "{company_id}"',
                'city: "Москва"',
                'working_hours: "Пн-Пт 9:00-20:00"',
                'phone: "+7 000 000-00-00"',
                'address: "Адрес уточняется"',
                'website_url: "https://duplicate.example"',
                "telegram_url:",
                "lead_webhook_url:",
                "allowed_domains:",
                '  - "duplicate.example"',
                "allowed_topics:",
                '  - "услуги"',
                '  - "цены"',
                '  - "запись"',
                "operator_triggers:",
                '  - "оператор"',
                '  - "специалист"',
                "forbidden_claims:",
                '  - "гарантия результата"',
                'safety_disclaimer: "По этому вопросу лучше уточнить у специалиста."',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_medical_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "domain_profile:",
                '  type: "medical"',
                "  restricted_advice:",
                '    - "medical"',
                '    - "diagnosis"',
                '    - "treatment"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _prepare_clients_dir(temp_dir: Path) -> Path:
    clients_dir = temp_dir / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)

    rosh_dir = clients_dir / "rosh_demo"
    rosh_dir.mkdir()
    for file_name in REQUIRED_KB_FILES:
        shutil.copy2(REPO_ROOT / "backend" / "data" / file_name, rosh_dir / file_name)
    _write_medical_config(rosh_dir / "config.yaml")

    template_dir = REPO_ROOT / "backend" / "data" / "client_template" / "medical_sample"
    for company_id in ("dup_one", "dup_two"):
        target_dir = clients_dir / company_id
        shutil.copytree(template_dir, target_dir)
        _replace_company_id(target_dir / "company.yaml", company_id)
        _write_duplicate_company(target_dir / "company.yaml", company_id)

    return clients_dir


def _configure_env(temp_dir: Path, clients_dir: Path) -> None:
    os.environ.update(
        {
            "LLM_PROVIDER": "mock",
            "DEV_MODE": "true",
            "DEFAULT_COMPANY_ID": "rosh_demo",
            "CLIENTS_DATA_DIR": str(clients_dir),
            "LEADS_FILE": str(temp_dir / "leads.jsonl"),
            "ANALYTICS_FILE": str(temp_dir / "analytics.jsonl"),
            "DELIVERY_OUTBOX_FILE": str(temp_dir / "delivery_outbox.jsonl"),
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "OPERATOR_TOKEN": "demo-operator-token",
        }
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="chatbot-managed-smoke-") as temp:
        temp_dir = Path(temp)
        clients_dir = _prepare_clients_dir(temp_dir)
        _configure_env(temp_dir, clients_dir)

        from app.config import get_settings  # noqa: WPS433

        get_settings.cache_clear()

        from fastapi.testclient import TestClient  # noqa: WPS433
        from app.main import app  # noqa: WPS433

        runner = SmokeRunner()
        with TestClient(app) as client:
            runner.check(
                "bootstrap explicit",
                "200",
                lambda: client.get(
                    "/api/widget/bootstrap?company_id=rosh_demo",
                    headers={"origin": "http://localhost:5500"},
                ).status_code
                == 200,
            )
            runner.check(
                "bootstrap features",
                "defaults",
                lambda: client.get(
                    "/api/widget/bootstrap?company_id=rosh_demo",
                    headers={"origin": "http://localhost:5500"},
                ).json()
                .get("features")
                == {"operator": True, "lead_capture": True, "analytics": False},
            )
            runner.check(
                "bootstrap autodetect",
                "200",
                lambda: client.get(
                    "/api/widget/bootstrap",
                    headers={"origin": "http://localhost:5500"},
                ).json()
                .get("company_id")
                == "rosh_demo",
            )
            runner.check(
                "bootstrap unknown",
                "404",
                lambda: client.get(
                    "/api/widget/bootstrap?company_id=nonexistent",
                    headers={"origin": "http://localhost:5500"},
                ).status_code
                == 404,
            )
            runner.check(
                "bootstrap wrong domain",
                "403",
                lambda: client.get(
                    "/api/widget/bootstrap?company_id=rosh_demo",
                    headers={"origin": "https://wrong.example"},
                ).status_code
                == 403,
            )
            runner.check(
                "chat list services",
                "200",
                lambda: bool(
                    client.post(
                        "/api/chat/message",
                        json={"company_id": "rosh_demo", "session_id": None, "message": "покажи услуги"},
                    ).json()
                    .get("action")
                ),
            )
            runner.check(
                "chat price",
                "200",
                lambda: client.post(
                    "/api/chat/message",
                    json={
                        "company_id": "rosh_demo",
                        "session_id": None,
                        "message": "сколько стоит чистка лица",
                    },
                ).status_code
                == 200,
            )
            runner.check(
                "chat regulated handoff",
                "transfer_operator",
                lambda: client.post(
                    "/api/chat/message",
                    json={"company_id": "rosh_demo", "session_id": None, "message": "у меня болит"},
                ).json()
                .get("action")
                == "transfer_operator",
            )
            runner.check(
                "lead jsonl company_id",
                "rosh_demo",
                lambda: (
                    client.post(
                        "/api/leads",
                        json={
                            "company_id": "rosh_demo",
                            "session_id": "smoke-session",
                            "name": "Иван",
                            "phone": "+7 999 123-45-67",
                            "summary": "Хочу записаться",
                            "service_id": "facial_cleaning",
                        },
                    ).status_code
                    == 200
                    and read_jsonl(temp_dir / "leads.jsonl")[-1].get("company_id") == "rosh_demo"
                ),
            )
            # per-client leads: глобальный logs/leads.jsonl с company_id достаточен
            # для managed MVP. Per-client файлы — V2/PostgreSQL.
            runner.check(
                "analytics summary",
                "200",
                lambda: client.get(
                    "/api/analytics/summary?company_id=rosh_demo",
                    headers={"x-operator-token": "demo-operator-token"},
                ).status_code
                == 200,
            )
            runner.check(
                "duplicate domain",
                "409",
                lambda: client.get(
                    "/api/widget/bootstrap",
                    headers={"origin": "https://duplicate.example"},
                ).status_code
                == 409,
            )

        print(f"\nИТОГО: {runner.passed}/{runner.total} прошло")
        return 0 if runner.passed == runner.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
