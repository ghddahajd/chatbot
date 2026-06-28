"""общие fixtures для backend-тестов."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
REQUIRED_KB_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")

sys.path.insert(0, str(BACKEND_DIR))


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
                'medical_disclaimer: "Я не врач и не ставлю диагнозы."',
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
        shutil.copy2(BACKEND_DIR / "data" / file_name, rosh_dir / file_name)

    template_dir = BACKEND_DIR / "data" / "client_template" / "sample_client"
    for company_id in ("dup_one", "dup_two"):
        target_dir = clients_dir / company_id
        shutil.copytree(template_dir, target_dir)
        _replace_company_id(target_dir / "company.yaml", company_id)
        _write_duplicate_company(target_dir / "company.yaml", company_id)

    return clients_dir


@pytest.fixture()
def managed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    clients_dir = _prepare_clients_dir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DEFAULT_COMPANY_ID", "rosh_demo")
    monkeypatch.setenv("CLIENTS_DATA_DIR", str(clients_dir))
    monkeypatch.setenv("LEADS_FILE", str(tmp_path / "leads.jsonl"))
    monkeypatch.setenv("ANALYTICS_FILE", str(tmp_path / "analytics.jsonl"))
    monkeypatch.setenv("DELIVERY_OUTBOX_FILE", str(tmp_path / "delivery_outbox.jsonl"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("OPERATOR_TOKEN", "demo-operator-token")

    from app.config import get_settings

    get_settings.cache_clear()
    yield {"temp_dir": tmp_path, "clients_dir": clients_dir}
    get_settings.cache_clear()


@pytest.fixture()
def resolver(managed_env: dict[str, Path]):
    from app.config import BASE_DIR
    from app.knowledge import KnowledgeBaseResolver

    return KnowledgeBaseResolver(
        data_dir=BASE_DIR / "data",
        clients_data_dir=managed_env["clients_dir"],
        defaults_data_dir=BASE_DIR / "data" / "defaults",
        default_company_id="rosh_demo",
    )


@pytest.fixture()
def knowledge_base(resolver):
    return resolver.get("rosh_demo", fallback=False)


@pytest.fixture()
def policy_session():
    from app.models import Session

    return Session(company_id="rosh_demo")


@pytest.fixture()
def test_client(managed_env: dict[str, Path]):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client
