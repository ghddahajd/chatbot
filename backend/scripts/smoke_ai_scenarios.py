"""smoke-матрица AI/policy сценариев для любого опубликованного клиента."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


SPECIAL_REASONS = {
    "cosmetic_concern",
    "lead_created",
    "list_services",
    "medical",
    "operator_soft",
    "price",
}


def _service_name(service: Any, fallback: str) -> str:
    return str(getattr(service, "name", "") or fallback).strip()


def _build_scenarios(knowledge_base: Any) -> list[tuple[str, str, str | None]]:
    services = list(getattr(knowledge_base, "services", []) or [])
    first_service = _service_name(services[0], "первая услуга") if services else "первая услуга"
    second_service = _service_name(services[1], first_service) if len(services) > 1 else first_service

    return [
        # Приветствие
        ("привет", "small_talk", None),
        ("добрый день", "small_talk", None),
        # Список услуг
        ("покажи услуги", "list_services", None),
        ("что у вас есть", "list_services", None),
        ("а можно услуги", "list_services", None),
        # Цена (есть в KB)
        (f"сколько стоит {first_service}", "answer", "price"),
        (f"цена на {second_service}", "answer", "price"),
        # Цена (услуга не названа)
        ("сколько стоит?", "clarify", "price_question_no_service"),
        # Цена (нет в KB)
        ("сколько стоит ботокс", "clarify", "unknown_service"),
        # Длительность
        ("сколько длится процедура", "clarify", "unknown_service"),
        (f"как долго {first_service}", "answer", None),
        # Неизвестная услуга
        ("есть ботокс?", "clarify", "unknown_service"),
        ("делаете татуаж?", "clarify", "unknown_service"),
        # Описание услуги
        (f"расскажи про {first_service}", "answer", None),
        (f"что входит в {second_service}", "answer", None),
        # Запись/лид
        ("хочу записаться", "clarify", "booking_request"),
        (f"Иван +7 999 123-45-67 хочу на {first_service}", "ask_contact", "lead_created"),
        # Оператор
        ("позовите оператора", "clarify", "operator_soft"),
        ("хочу живого человека", "clarify", "operator_soft"),
        ("да оператора", "transfer_operator", None),
        # Медицина/опасное
        ("у меня воспаление что делать", "transfer_operator", "medical"),
        ("выпишите мне крем", "transfer_operator", "medical"),
        ("болит после процедуры", "transfer_operator", "medical"),
        # Geography
        ("я из Новосибирска можно?", "clarify", "location_mismatch"),
        ("работаете в Питере?", "clarify", "location_mismatch"),
        # Off-topic
        ("слетела цепь на велике", "off_topic", None),
        ("какая погода", "off_topic", None),
    ]
POLICY_ACTIONS = {
    "answer",
    "ask_contact",
    "clarify",
    "off_topic",
    "reject",
    "small_talk",
    "transfer_operator",
}


@dataclass
class ScenarioResult:
    message: str
    expected_action: str
    expected_marker: str | None
    got_action: str
    got_marker: str | None
    ok: bool

    @property
    def got_outcome(self) -> str:
        if self.expected_action not in POLICY_ACTIONS and self.got_marker:
            return self.got_marker
        return self.got_action


def _configure_env(company_id: str, use_real_llm: bool, temp_dir: Path) -> None:
    os.environ.setdefault("DEV_MODE", "true")
    os.environ.setdefault("DEFAULT_COMPANY_ID", company_id)
    os.environ.setdefault("CLIENTS_DATA_DIR", str(BACKEND_DIR / "data" / "clients"))
    os.environ.setdefault("OPERATOR_TOKEN", "demo-operator-token")
    os.environ["LEADS_FILE"] = str(temp_dir / "leads.jsonl")
    os.environ["ANALYTICS_FILE"] = str(temp_dir / "analytics.jsonl")
    os.environ["DELIVERY_OUTBOX_FILE"] = str(temp_dir / "delivery_outbox.jsonl")
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_CHAT_ID"] = ""
    if not use_real_llm:
        os.environ["LLM_PROVIDER"] = "mock"
        os.environ["LLM_API_KEY"] = ""
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["GEMINI_API_KEY"] = ""


def _policy_marker(policy_result: Any, classification: dict[str, object]) -> str | None:
    reason = str(getattr(policy_result, "reason", "") or "")
    reason_value = reason.split(".")[-1]
    if reason_value:
        reason = str(getattr(policy_result.reason, "value", reason_value))

    safe_context = getattr(policy_result, "safe_context", {}) or {}
    if safe_context.get("question_type") == "list_services":
        return "list_services"
    if safe_context.get("question_type") == "price":
        return "price"
    if safe_context.get("question_type") == "cosmetic_concern":
        return "cosmetic_concern"
    if reason == "medical_advice":
        return "medical"
    if reason == "operator_requested":
        return "operator_soft"
    if reason:
        return reason
    intent = str(classification.get("intent") or "")
    return intent or None


def _matches_expected(result: ScenarioResult) -> bool:
    if result.got_outcome != result.expected_action:
        return False
    if result.expected_marker is None:
        return True
    return result.got_marker == result.expected_marker


def _format_label(message: str) -> str:
    return f'"{message}"'


def _print_result(result: ScenarioResult) -> None:
    marker = "✅" if result.ok else "❌"
    suffix = f" ({result.got_marker})" if result.got_marker and result.got_marker != result.got_outcome else ""
    print(f"{marker} {_format_label(result.message):<34} → {result.got_outcome}{suffix}")
    if not result.ok:
        expected_suffix = f" ({result.expected_marker})" if result.expected_marker else ""
        print(f"   got: action={result.got_action} marker={result.got_marker or '-'}")
        print(f"   expected: action={result.expected_action}{expected_suffix}")


async def _policy_result_for_message(message: str, session: Any, request: Any, knowledge_base: Any) -> tuple[Any, dict[str, object]]:
    from app.models import Message, MessageRole  # noqa: WPS433
    from app.routes.chat_utils import resolve_classification  # noqa: WPS433

    session.messages.append(Message(role=MessageRole.USER, text=message))
    classification = await resolve_classification(message, request, knowledge_base)
    policy_result = request.app.state.policy_analyzer(message, session, knowledge_base, classification)
    return policy_result, classification


def _chat_lead_result(client: Any, company_id: str, message: str) -> ScenarioResult:
    initial_response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": None, "message": "хочу записаться"},
    )
    session_id = initial_response.json().get("session_id")
    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": message},
    )
    payload = response.json()
    got_action = str(payload.get("action") or f"http_{response.status_code}")
    got_marker = "lead_created" if payload.get("lead_created") is True else None
    result = ScenarioResult(message, "ask_contact", "lead_created", got_action, got_marker, False)
    result.ok = _matches_expected(result)
    return result


def _resolve_runner_label(use_real_llm: bool) -> str:
    from app.config import get_settings  # noqa: WPS433

    settings = get_settings()
    if use_real_llm:
        return f"{settings.llm_provider} / {settings.llm_model}"
    return "MockLLMClient (no API key)"


async def _run(company_id: str, use_real_llm: bool, temp_dir: Path) -> list[ScenarioResult]:
    from fastapi import Request  # noqa: WPS433
    from fastapi.testclient import TestClient  # noqa: WPS433

    from app.config import get_settings  # noqa: WPS433

    get_settings.cache_clear()

    from app.main import app  # noqa: WPS433
    from app.models import Message, MessageRole, Session  # noqa: WPS433
    from app.policy.constants import OPERATOR_SOFT_OFFER_MESSAGE  # noqa: WPS433

    results: list[ScenarioResult] = []
    with TestClient(app) as client:
        knowledge_base = app.state.knowledge_base_resolver.get(company_id, fallback=False)
        scenarios = _build_scenarios(knowledge_base)
        request = Request({"type": "http", "app": app})

        for message, expected_action, expected_marker in scenarios:
            if expected_marker == "lead_created":
                result = _chat_lead_result(client, company_id, message)
                results.append(result)
                continue

            session = Session(company_id=company_id)
            if message == "да оператора":
                session.messages.append(
                    Message(role=MessageRole.ASSISTANT, text=OPERATOR_SOFT_OFFER_MESSAGE)
                )
            policy_result, classification = await _policy_result_for_message(
                message,
                session,
                request,
                knowledge_base,
            )
            got_action = str(policy_result.action.value)
            got_marker = _policy_marker(policy_result, classification)
            if (
                expected_marker is None
                and expected_action in POLICY_ACTIONS
                and got_marker in SPECIAL_REASONS
            ):
                got_marker = None
            result = ScenarioResult(message, expected_action, expected_marker, got_action, got_marker, False)
            result.ok = _matches_expected(result)
            results.append(result)

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прогнать smoke-матрицу AI/policy сценариев.")
    parser.add_argument("--company", default="rosh_demo", help="company_id опубликованного клиента")
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="не принуждать mock provider; использовать LLM-настройки окружения",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="chatbot-ai-smoke-") as temp:
        temp_dir = Path(temp)
        _configure_env(args.company, args.real_llm, temp_dir)

        import anyio

        print(f"AI Conversation Smoke Test — {args.company}")
        print(_resolve_runner_label(args.real_llm))
        print("─" * 40)
        results = anyio.run(_run, args.company, args.real_llm, temp_dir)
        for result in results:
            _print_result(result)

        passed = sum(1 for result in results if result.ok)
        print("─" * 40)
        print(f"ИТОГО: {passed}/{len(results)} прошло")

        failed = [result for result in results if not result.ok]
        if failed:
            print("\nПровалившиеся:")
            for result in failed:
                got_suffix = f" ({result.got_marker})" if result.got_marker else ""
                expected_suffix = f" ({result.expected_marker})" if result.expected_marker else ""
                print(
                    f"❌ {_format_label(result.message)} → got: {result.got_action}{got_suffix}"
                    f" expected: {result.expected_action}{expected_suffix}"
                )

        return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
