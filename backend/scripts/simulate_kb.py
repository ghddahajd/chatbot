"""прогоняет тестовые вопросы на клиентской KB без запуска Docker."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))

from validate_kb import load_simple_yaml, validate_kb  # noqa: E402


DEFAULT_QUESTIONS = [
    "привет",
    "покажи услуги",
    "сколько стоит консультация",
    "есть ботокс?",
    "что пить от прыщей?",
    "я не из вашего города",
    "позовите оператора",
]


def _company_id_from(kb_dir: Path) -> str:
    company = load_simple_yaml(kb_dir / "company.yaml")
    company_id = str(company.get("company_id") or "").strip()
    if not company_id:
        raise ValueError("company.yaml: пустой company_id")
    return company_id


def simulate(kb_dir: Path, questions: list[str]) -> int:
    errors = validate_kb(kb_dir)
    if errors:
        print("KB validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    company_id = _company_id_from(kb_dir)
    with tempfile.TemporaryDirectory(prefix="chat-widget-kb-sim-") as temp_dir:
        clients_dir = Path(temp_dir) / "clients"
        target_dir = clients_dir / company_id
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(kb_dir, target_dir)

        os.environ["LLM_PROVIDER"] = "mock"
        os.environ["CLIENTS_DATA_DIR"] = str(clients_dir)
        os.environ["DEFAULT_COMPANY_ID"] = company_id
        os.environ["LEADS_FILE"] = str(Path(temp_dir) / "leads.jsonl")
        os.environ["ANALYTICS_FILE"] = str(Path(temp_dir) / "analytics.jsonl")
        os.environ["DELIVERY_OUTBOX_FILE"] = str(Path(temp_dir) / "delivery_outbox.jsonl")

        try:
            from app.config import get_settings  # noqa: WPS433
        except ModuleNotFoundError as error:
            print(
                "Не хватает backend-зависимостей для simulator. "
                "Запусти: cd backend && pip install -r requirements.txt",
                file=sys.stderr,
            )
            print(f"missing module: {error.name}", file=sys.stderr)
            return 1

        get_settings.cache_clear()

        from fastapi.testclient import TestClient  # noqa: WPS433
        from app.main import app  # noqa: WPS433

        with TestClient(app) as client:
            session_id = None
            for question in questions:
                response = client.post(
                    "/api/chat/message",
                    json={
                        "company_id": company_id,
                        "session_id": session_id,
                        "message": question,
                    },
                )
                print(f"\n> {question}")
                print(f"status: {response.status_code}")
                if response.status_code != 200:
                    print(response.text)
                    continue
                payload = response.json()
                session_id = payload.get("session_id") or session_id
                print(f"action: {payload.get('action')} / session: {payload.get('status')}")
                print(payload.get("answer"))
                quick_actions = payload.get("quick_actions") or []
                if quick_actions:
                    labels = ", ".join(str(item.get("label")) for item in quick_actions)
                    print(f"quick_actions: {labels}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогнать вопросы на KB клиента.")
    parser.add_argument("kb_dir", type=Path, help="Папка KB клиента или draft")
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="Вопрос для проверки. Можно указать несколько раз.",
    )
    args = parser.parse_args()

    questions = args.question or DEFAULT_QUESTIONS
    return simulate(args.kb_dir, questions)


if __name__ == "__main__":
    raise SystemExit(main())
