"""проверяет готовность опубликованного клиента к запуску."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


@dataclass
class CheckState:
    blockers: list[str]
    warnings: list[str]

    def ok(self, message: str) -> None:
        print(f"  ✅ {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"  ⚠️  {message}")

    def block(self, message: str) -> None:
        self.blockers.append(message)
        print(f"  ❌ БЛОКЕР: {message}")


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _configure_env(company_id: str, temp_dir: Path) -> None:
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["LLM_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["DEV_MODE"] = "true"
    os.environ["DEFAULT_COMPANY_ID"] = company_id
    os.environ.setdefault("CLIENTS_DATA_DIR", str(BACKEND_DIR / "data" / "clients"))
    os.environ["LEADS_FILE"] = str(temp_dir / "leads.jsonl")
    os.environ["ANALYTICS_FILE"] = str(temp_dir / "analytics.jsonl")
    os.environ["DELIVERY_OUTBOX_FILE"] = str(temp_dir / "delivery_outbox.jsonl")
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_CHAT_ID"] = ""
    os.environ.setdefault("OPERATOR_TOKEN", "demo-operator-token")


def _first_service_name(services: list[dict[str, Any]]) -> str:
    for service in services:
        name = str(service.get("name") or "").strip()
        if name:
            return name
    return "первая услуга"


def _check_client_files(company_id: str, client_dir: Path, state: CheckState) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    print("KB:")
    if not client_dir.exists():
        state.block(f"клиент не найден: {client_dir}")
        return [], [], {}
    state.ok(f"клиент найден: {client_dir}")

    services_payload = _load_json(client_dir / "services.json")
    prices_payload = _load_json(client_dir / "prices.json")
    company = _load_yaml(client_dir / "company.yaml")
    faq_text = (client_dir / "faq.md").read_text(encoding="utf-8") if (client_dir / "faq.md").exists() else ""

    services = services_payload if isinstance(services_payload, list) else []
    prices = prices_payload if isinstance(prices_payload, list) else []

    if not services:
        state.block("services.json пустой или не читается")
    elif len(services) < 3:
        state.warn(f"мало услуг: {len(services)}")
    else:
        state.ok(f"услуг: {len(services)}")

    if not prices:
        state.warn("нет цен в prices.json")
    else:
        state.ok(f"цен: {len(prices)}")

    faq_lines = [line for line in faq_text.splitlines() if line.strip()]
    if len(faq_lines) < 3:
        state.warn(f"FAQ слишком короткий ({len(faq_lines)} строк, рекомендуется 3+)")
    else:
        state.ok(f"FAQ строк: {len(faq_lines)}")

    price_ids = {str(price.get("service_id")) for price in prices if isinstance(price, dict)}
    missing_price_names = [
        str(service.get("name") or service.get("id"))
        for service in services
        if isinstance(service, dict) and str(service.get("id")) not in price_ids
    ]
    if missing_price_names:
        state.warn("услуги без цены: " + ", ".join(missing_price_names))
    elif services:
        state.ok("цены есть для всех услуг")

    print("\nКонфиг:")
    if company.get("company_name"):
        state.ok(f"company_name: {company['company_name']}")
    else:
        state.block("company_name не заполнен")

    if company.get("city"):
        state.ok(f"city: {company['city']}")
    else:
        state.warn("city не заполнен")

    if company.get("website_url"):
        state.ok(f"website_url: {company['website_url']}")
    else:
        state.warn("website_url пустой — кнопка сайта не будет работать")

    if company.get("telegram_url"):
        state.ok(f"telegram_url: {company['telegram_url']}")
    else:
        state.warn("telegram_url пустой — кнопка TG не будет работать")

    allowed_domains = [
        str(domain).strip()
        for domain in (company.get("allowed_domains") or [])
        if str(domain).strip()
    ]
    if not allowed_domains:
        state.block("allowed_domains пустой")
    elif any("localhost" not in domain and "127.0.0.1" not in domain for domain in allowed_domains):
        state.ok("allowed_domains: " + ", ".join(allowed_domains))
    else:
        state.warn("allowed_domains содержит только localhost")

    return services, prices, company


def _run_api_checks(company_id: str, services: list[dict[str, Any]], temp_dir: Path, state: CheckState) -> None:
    from app.config import get_settings  # noqa: WPS433

    get_settings.cache_clear()

    from fastapi.testclient import TestClient  # noqa: WPS433
    from app.main import app  # noqa: WPS433

    with TestClient(app) as client:
        print("\nBootstrap:")
        bootstrap = client.get(f"/api/widget/bootstrap?company_id={company_id}")
        if bootstrap.status_code != 200:
            state.block(f"bootstrap вернул {bootstrap.status_code}")
            return

        payload = bootstrap.json()
        state.ok("200 OK")
        if payload.get("features"):
            features = payload["features"]
            state.ok(
                "features: "
                + ", ".join(f"{key}={value}" for key, value in sorted(features.items()))
            )
        else:
            state.warn("features отсутствует в bootstrap response")

        if payload.get("widget_config"):
            state.ok("widget_config есть")
        else:
            state.warn("widget_config отсутствует в bootstrap response")

        print("\nChat smoke:")
        list_response = client.post(
            "/api/chat/message",
            json={"company_id": company_id, "session_id": None, "message": "покажи услуги"},
        )
        list_payload = list_response.json()
        if list_response.status_code == 200 and list_payload.get("action"):
            state.ok("список услуг работает")
        else:
            state.block("список услуг не работает")

        first_service_name = _first_service_name(services)
        price_response = client.post(
            "/api/chat/message",
            json={
                "company_id": company_id,
                "session_id": None,
                "message": f"сколько стоит {first_service_name}",
            },
        )
        price_payload = price_response.json()
        if price_response.status_code == 200 and price_payload.get("answer"):
            answer = str(price_payload.get("answer") or "")
            if "Предварительно" in answer:
                state.ok("цена с оговоркой")
            else:
                state.warn("цена отвечает, но без стандартной оговорки")
        else:
            state.warn("вопрос о цене не дал ответ")

        medical_response = client.post(
            "/api/chat/message",
            json={"company_id": company_id, "session_id": None, "message": "у меня болит"},
        )
        medical_payload = medical_response.json()
        if medical_payload.get("action") == "transfer_operator":
            state.ok("медицинский вопрос → transfer_operator")
        else:
            state.block("медицинский вопрос не защищён")

        print("\nLeads:")
        lead_response = client.post(
            "/api/leads",
            json={
                "company_id": company_id,
                "session_id": "launch-check",
                "name": "Тест",
                "phone": "+7 999 123-45-67",
                "summary": "Проверка запуска клиента",
                "service_id": services[0].get("id") if services else None,
            },
        )
        leads = _read_jsonl(temp_dir / "leads.jsonl")
        if (
            lead_response.status_code == 200
            and leads
            and leads[-1].get("company_id") == company_id
        ):
            state.ok("company_id сохраняется")
        else:
            state.block("лид не сохранился с нужным company_id")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверить готовность клиента к запуску.")
    parser.add_argument("--company", required=True, help="company_id опубликованного клиента")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="chatbot-client-launch-") as temp:
        temp_dir = Path(temp)
        _configure_env(args.company, temp_dir)

        from app.config import get_settings  # noqa: WPS433

        get_settings.cache_clear()
        settings = get_settings()
        client_dir = settings.clients_data_dir / args.company

        state = CheckState(blockers=[], warnings=[])
        print("══════════════════════════════════════")
        print(f"Client Launch Check: {args.company}")
        print("══════════════════════════════════════")

        services, _prices, _company = _check_client_files(args.company, client_dir, state)
        if state.blockers:
            print("\nBootstrap / Chat smoke / Leads:")
            state.warn("API-проверки пропущены из-за блокеров в KB или конфиге")
        else:
            _run_api_checks(args.company, services, temp_dir, state)

        print("\n══════════════════════════════════════")
        if state.blockers:
            print(f"ИТОГО: ❌ есть блокеры ({len(state.blockers)})")
            for blocker in state.blockers:
                print(f"       - {blocker}")
            if state.warnings:
                print(f"       предупреждений: {len(state.warnings)}")
            return 1

        if state.warnings:
            print(f"ИТОГО: ✅ готов к запуску ({len(state.warnings)} предупреждения)")
            for warning in state.warnings:
                print(f"       - {warning}")
            return 0

        print("ИТОГО: ✅ готов к запуску")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
