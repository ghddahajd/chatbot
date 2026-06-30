"""baseline eval runner для качества AI/policy понимания фраз."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
EVALS_DIR = BACKEND_DIR / "evals"
POLICY_ACTIONS = {
    "answer",
    "ask_contact",
    "clarify",
    "off_topic",
    "reject",
    "small_talk",
    "transfer_operator",
}
SPECIAL_MARKERS = {
    "booking_request",
    "contact_provided",
    "cosmetic_concern",
    "lead_created",
    "list_services",
    "location_mismatch",
    "medical",
    "operator_soft",
    "price",
    "price_question_no_service",
    "unknown_service",
}

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class EvalCase:
    id: str
    source: str
    company: str | None
    message: str
    history: list[dict[str, str]]
    expected_intent: str | None
    expected_action: str | None
    expected_marker: str | None
    expected_service_id: str | None


@dataclass
class EvalResult:
    case: EvalCase
    got_intent: str | None
    got_action: str
    got_marker: str | None
    got_service_id: str | None
    ok: bool


def _configure_env(company_id: str, use_real_llm: bool, temp_dir: Path, intent_engine: str) -> None:
    os.environ.setdefault("DEV_MODE", "true")
    os.environ.setdefault("DEFAULT_COMPANY_ID", company_id)
    os.environ.setdefault("CLIENTS_DATA_DIR", str(BACKEND_DIR / "data" / "clients"))
    os.environ.setdefault("OPERATOR_TOKEN", "demo-operator-token")
    os.environ["LEADS_FILE"] = str(temp_dir / "leads.jsonl")
    os.environ["ANALYTICS_FILE"] = str(temp_dir / "analytics.jsonl")
    os.environ["DELIVERY_OUTBOX_FILE"] = str(temp_dir / "delivery_outbox.jsonl")
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    os.environ["TELEGRAM_CHAT_ID"] = ""
    os.environ["LLM_USE_STRUCTURED_CLASSIFIER"] = "true" if intent_engine == "structured" else "false"
    if not use_real_llm:
        os.environ["LLM_PROVIDER"] = "mock"
        os.environ["LLM_API_KEY"] = ""
        os.environ["OPENAI_API_KEY"] = ""
        os.environ["GEMINI_API_KEY"] = ""


def _apply_llm_overrides(args: argparse.Namespace) -> None:
    if args.llm_provider:
        os.environ["LLM_PROVIDER"] = args.llm_provider
    if args.llm_base_url:
        os.environ["LLM_BASE_URL"] = args.llm_base_url
    if args.llm_model:
        os.environ["LLM_MODEL"] = args.llm_model
    if args.llm_api_key:
        os.environ["LLM_API_KEY"] = args.llm_api_key


def _print_llm_config(settings: Any, use_real_llm: bool, intent_engine: str) -> None:
    mode = "real" if use_real_llm else "mock"
    print(f"provider: {mode}")
    print(f"intent_engine: {intent_engine}")
    print(f"llm_provider: {settings.llm_provider}")
    print(f"llm_model: {settings.llm_model}")
    print(f"llm_base_url: {settings.llm_base_url}")
    print(f"structured_classifier: {settings.llm_use_structured_classifier}")


def _case_context(knowledge_base: Any) -> dict[str, str]:
    services = list(getattr(knowledge_base, "services", []) or [])
    first_service = services[0] if services else None
    second_service = services[1] if len(services) > 1 else first_service
    company_city = str(getattr(knowledge_base.company, "city", "") or "город")
    city_from_forms = {
        "Москва": "москвы",
        "Санкт-Петербург": "санкт-петербурга",
    }
    return {
        "first_service": str(getattr(first_service, "name", "") or "первая услуга"),
        "first_service_id": str(getattr(first_service, "id", "") or ""),
        "second_service": str(getattr(second_service, "name", "") or "вторая услуга"),
        "second_service_id": str(getattr(second_service, "id", "") or ""),
        "company_city": company_city,
        "company_city_from": city_from_forms.get(company_city, company_city),
    }


def _format_value(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**context)
    return value


def _load_jsonl(path: Path, context: dict[str, str], company_id: str) -> list[EvalCase]:
    cases: list[EvalCase] = []
    if not path.exists():
        return cases

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw_case = json.loads(line)
        case_company = raw_case.get("company")
        if case_company not in {None, "all", company_id}:
            continue
        history = raw_case.get("history") or []
        if not isinstance(history, list):
            raise ValueError(f"{path}:{line_number}: history должен быть list")
        formatted_history = [
            {
                "role": str(item.get("role") or "user"),
                "text": str(_format_value(item.get("text") or "", context)),
            }
            for item in history
            if isinstance(item, dict)
        ]
        cases.append(
            EvalCase(
                id=str(raw_case.get("id") or f"{path.stem}:{line_number}"),
                source=path.name,
                company=str(case_company) if case_company else None,
                message=str(_format_value(raw_case.get("message") or "", context)),
                history=formatted_history,
                expected_intent=str(_format_value(raw_case.get("expected_intent"), context))
                if raw_case.get("expected_intent") is not None
                else None,
                expected_action=str(_format_value(raw_case.get("expected_action"), context))
                if raw_case.get("expected_action") is not None
                else None,
                expected_marker=str(_format_value(raw_case.get("expected_marker"), context))
                if raw_case.get("expected_marker") is not None
                else None,
                expected_service_id=str(_format_value(raw_case.get("expected_service_id"), context))
                if raw_case.get("expected_service_id") is not None
                else None,
            )
        )
    return cases


def _load_cases(company_id: str, knowledge_base: Any) -> list[EvalCase]:
    context = _case_context(knowledge_base)
    paths = [
        EVALS_DIR / "universal.jsonl",
        EVALS_DIR / "edge_cases.jsonl",
        EVALS_DIR / f"{company_id}.jsonl",
    ]
    cases: list[EvalCase] = []
    for path in paths:
        cases.extend(_load_jsonl(path, context, company_id))
    return cases


def _policy_marker(policy_result: Any, classification: dict[str, object]) -> str | None:
    reason = str(getattr(policy_result.reason, "value", "") or "")
    safe_context = getattr(policy_result, "safe_context", {}) or {}
    if safe_context.get("question_type") == "list_services":
        return "list_services"
    if safe_context.get("question_type") == "price":
        return "price"
    if safe_context.get("question_type") == "cosmetic_concern":
        return "cosmetic_concern"
    if safe_context.get("contact"):
        return "contact_provided"
    if reason in {"medical_advice", "regulated_advice"}:
        return "medical"
    if reason == "operator_requested":
        return "operator_soft"
    if reason in SPECIAL_MARKERS:
        return reason
    return str(classification.get("intent") or "") or None


def _build_session(company_id: str, case: EvalCase) -> Any:
    from app.models import Message, MessageRole, Session  # noqa: WPS433

    session = Session(company_id=company_id)
    for item in case.history:
        role = MessageRole.ASSISTANT if item["role"] == "assistant" else MessageRole.USER
        session.messages.append(Message(role=role, text=item["text"]))
    return session


async def _evaluate_policy_case(app: Any, company_id: str, case: EvalCase, knowledge_base: Any) -> EvalResult:
    from fastapi import Request  # noqa: WPS433
    from app.models import Message, MessageRole  # noqa: WPS433
    from app.routes.chat_utils import (  # noqa: WPS433
        contextual_affirmative_response,
        maybe_contextual_classification,
        resolve_classification,
    )

    session = _build_session(company_id, case)
    session.messages.append(Message(role=MessageRole.USER, text=case.message))
    request = Request({"type": "http", "app": app})
    policy_result = contextual_affirmative_response(case.message, session)
    if policy_result is None:
        classification = maybe_contextual_classification(case.message, session)
        if classification is None:
            classification = await resolve_classification(case.message, request, knowledge_base)
        policy_result = app.state.policy_analyzer(case.message, session, knowledge_base, classification)
    else:
        classification = {"intent": "contextual_affirmative", "service_id": None, "confidence": 0.9}
    got_action = str(policy_result.action.value)
    got_service_id = str(policy_result.service_id or classification.get("service_id") or "") or None
    got_marker = _policy_marker(policy_result, classification)
    result = EvalResult(
        case=case,
        got_intent=str(classification.get("intent") or "") or None,
        got_action=got_action,
        got_marker=got_marker,
        got_service_id=got_service_id,
        ok=False,
    )
    result.ok = _matches(result)
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _evaluate_chat_case(client: Any, company_id: str, case: EvalCase, leads_file: Path) -> EvalResult:
    session_id = None
    for item in case.history:
        if item["role"] != "user":
            continue
        response = client.post(
            "/api/chat/message",
            json={"company_id": company_id, "session_id": session_id, "message": item["text"]},
        )
        session_id = response.json().get("session_id")

    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": case.message},
    )
    payload = response.json()
    leads = _read_jsonl(leads_file)
    lead_service_id = None
    if leads:
        lead_service_id = str(leads[-1].get("service_id") or "") or None
    result = EvalResult(
        case=case,
        got_intent=None,
        got_action=str(payload.get("action") or f"http_{response.status_code}"),
        got_marker="lead_created" if payload.get("lead_created") is True else None,
        got_service_id=lead_service_id,
        ok=False,
    )
    result.ok = _matches(result)
    return result


def _matches(result: EvalResult) -> bool:
    case = result.case
    if case.expected_intent is not None and result.got_intent != case.expected_intent:
        return False
    if case.expected_action is not None and result.got_action != case.expected_action:
        return False
    if case.expected_marker is not None and result.got_marker != case.expected_marker:
        return False
    if case.expected_service_id is not None and result.got_service_id != case.expected_service_id:
        return False
    return True


def _print_result(result: EvalResult) -> None:
    marker = "✅" if result.ok else "❌"
    details = [
        f"intent={result.got_intent or '-'}",
        f"action={result.got_action}",
        f"marker={result.got_marker or '-'}",
        f"service={result.got_service_id or '-'}",
    ]
    print(f"{marker} {result.case.id:<34} {result.case.message!r}")
    print("   got: " + ", ".join(details))
    if not result.ok:
        expected = [
            f"intent={result.case.expected_intent or '*'}",
            f"action={result.case.expected_action or '*'}",
            f"marker={result.case.expected_marker or '*'}",
            f"service={result.case.expected_service_id or '*'}",
        ]
        print("   exp: " + ", ".join(expected))


async def _run(company_id: str, use_real_llm: bool, temp_dir: Path) -> list[EvalResult]:
    from fastapi.testclient import TestClient  # noqa: WPS433
    from app.config import get_settings  # noqa: WPS433

    get_settings.cache_clear()

    from app.main import app  # noqa: WPS433

    results: list[EvalResult] = []
    with TestClient(app) as client:
        knowledge_base = app.state.knowledge_base_resolver.get(company_id, fallback=False)
        cases = _load_cases(company_id, knowledge_base)
        for case in cases:
            if case.expected_marker == "lead_created":
                results.append(_evaluate_chat_case(client, company_id, case, temp_dir / "leads.jsonl"))
                continue
            results.append(await _evaluate_policy_case(app, company_id, case, knowledge_base))
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прогнать AI quality eval baseline.")
    parser.add_argument("--company", default="rosh_demo")
    parser.add_argument(
        "--intent-engine",
        choices=("legacy", "structured"),
        default="structured",
        help="какой classifier path использовать для прогона",
    )
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--llm-provider", default="", help="override LLM_PROVIDER for this run")
    parser.add_argument("--llm-base-url", default="", help="override LLM_BASE_URL for this run")
    parser.add_argument("--llm-model", default="", help="override LLM_MODEL for this run")
    parser.add_argument("--llm-api-key", default="", help="override LLM_API_KEY for this run")
    parser.add_argument("--strict", action="store_true", help="вернуть exit 1, если есть провалы")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="chatbot-ai-evals-") as temp:
        temp_dir = Path(temp)
        _configure_env(args.company, args.real_llm, temp_dir, args.intent_engine)
        _apply_llm_overrides(args)

        import anyio

        from app.config import get_settings  # noqa: WPS433

        get_settings.cache_clear()

        print(f"AI Quality Evals — {args.company}")
        _print_llm_config(get_settings(), args.real_llm, args.intent_engine)
        print("─" * 60)
        results = anyio.run(_run, args.company, args.real_llm, temp_dir)
        for result in results:
            _print_result(result)

        passed = sum(1 for result in results if result.ok)
        total = len(results)
        print("─" * 60)
        print(f"ИТОГО: {passed}/{total} прошло")
        if passed != total:
            print("\nПровалившиеся:")
            for result in results:
                if not result.ok:
                    print(f"- {result.case.id}: {result.case.message}")
            return 1 if args.strict else 0
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
