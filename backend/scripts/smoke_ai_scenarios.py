"""smoke-матрица AI/policy сценариев для любого опубликованного клиента."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
EVALS_DIR = REPO_ROOT / "evals"

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


def _has_medical_restrictions(knowledge_base: Any) -> bool:
    domain_profile = getattr(knowledge_base, "domain_profile", {}) or {}
    if not isinstance(domain_profile, dict):
        return False
    domain_type = str(domain_profile.get("type") or "").strip().lower()
    restricted_advice = {
        str(category).strip().lower()
        for category in domain_profile.get("restricted_advice", [])
        if str(category).strip()
    }
    return domain_type == "medical" or bool(
        restricted_advice & {"medical", "medical_advice", "medical_treatment", "diagnosis", "treatment"}
    )


def _is_auto_domain(knowledge_base: Any) -> bool:
    domain_profile = getattr(knowledge_base, "domain_profile", {}) or {}
    if not isinstance(domain_profile, dict):
        return False
    return str(domain_profile.get("type") or "").strip().lower().startswith("auto")


@dataclass(frozen=True)
class Scenario:
    message: str
    expected_action: str
    expected_marker: str | None = None
    setup: str | None = None


@dataclass(frozen=True)
class ScenarioSet:
    name: str
    path: Path
    scenarios: list[Scenario]


def _scenario_context(knowledge_base: Any) -> dict[str, str]:
    services = list(getattr(knowledge_base, "services", []) or [])
    first_service = _service_name(services[0], "первая услуга") if services else "первая услуга"
    second_service = _service_name(services[1], first_service) if len(services) > 1 else first_service
    company_city = str(getattr(knowledge_base.company, "city", "") or "город")
    city_from_forms = {
        "Москва": "москвы",
        "Санкт-Петербург": "санкт-петербурга",
    }
    return {
        "first_service": first_service,
        "second_service": second_service,
        "company_city": company_city,
        "company_city_from": city_from_forms.get(company_city, company_city),
    }


def _load_eval_file(path: Path, context: dict[str, str]) -> list[Scenario]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
    if not isinstance(raw_scenarios, list):
        raise ValueError(f"{path} должен содержать list в поле scenarios")

    scenarios: list[Scenario] = []
    for index, raw_scenario in enumerate(raw_scenarios, start=1):
        if not isinstance(raw_scenario, dict):
            raise ValueError(f"{path}:{index} scenario должен быть object")
        message = str(raw_scenario.get("message") or "").format(**context).strip()
        expected_action = str(raw_scenario.get("action") or "").strip()
        expected_marker = raw_scenario.get("marker")
        setup = raw_scenario.get("setup")
        if not message or not expected_action:
            raise ValueError(f"{path}:{index} scenario требует message и action")
        scenarios.append(
            Scenario(
                message=message,
                expected_action=expected_action,
                expected_marker=str(expected_marker).strip() if expected_marker else None,
                setup=str(setup).strip() if setup else None,
            )
        )
    return scenarios


def _domain_eval_paths(knowledge_base: Any) -> list[Path]:
    paths: list[Path] = []
    if _has_medical_restrictions(knowledge_base):
        paths.append(EVALS_DIR / "domains" / "medical.yaml")
    if _is_auto_domain(knowledge_base):
        paths.append(EVALS_DIR / "domains" / "auto_service.yaml")
    return paths


def _build_scenario_sets(company_id: str, knowledge_base: Any) -> list[ScenarioSet]:
    context = _scenario_context(knowledge_base)
    scenario_sets = [
        ScenarioSet(
            name="universal",
            path=EVALS_DIR / "universal.yaml",
            scenarios=_load_eval_file(EVALS_DIR / "universal.yaml", context),
        )
    ]
    for path in _domain_eval_paths(knowledge_base):
        scenario_sets.append(
            ScenarioSet(
                name=f"domain:{path.stem}",
                path=path,
                scenarios=_load_eval_file(path, context),
            )
        )
    client_path = EVALS_DIR / "clients" / f"{company_id}.yaml"
    if client_path.exists():
        scenario_sets.append(
            ScenarioSet(
                name=f"client:{company_id}",
                path=client_path,
                scenarios=_load_eval_file(client_path, context),
            )
        )
    return scenario_sets


def _flatten_scenarios(scenario_sets: list[ScenarioSet]) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for scenario_set in scenario_sets:
        scenarios.extend(scenario_set.scenarios)
    return scenarios


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
    if reason in {"medical_advice", "regulated_advice"}:
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


def _chat_lead_result(client: Any, company_id: str, scenario: Scenario) -> ScenarioResult:
    initial_response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": None, "message": "хочу записаться"},
    )
    session_id = initial_response.json().get("session_id")
    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": scenario.message},
    )
    payload = response.json()
    got_action = str(payload.get("action") or f"http_{response.status_code}")
    got_marker = "lead_created" if payload.get("lead_created") is True else None
    result = ScenarioResult(
        scenario.message,
        scenario.expected_action,
        scenario.expected_marker,
        got_action,
        got_marker,
        False,
    )
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
    from app.policy.constants import CONTACT_PROMPT, OPERATOR_SOFT_OFFER_MESSAGE  # noqa: WPS433

    results: list[ScenarioResult] = []
    with TestClient(app) as client:
        knowledge_base = app.state.knowledge_base_resolver.get(company_id, fallback=False)
        scenario_sets = _build_scenario_sets(company_id, knowledge_base)
        scenarios = _flatten_scenarios(scenario_sets)
        request = Request({"type": "http", "app": app})
        app.state.ai_smoke_scenario_sets = scenario_sets

        for scenario in scenarios:
            if scenario.expected_marker == "lead_created":
                result = _chat_lead_result(client, company_id, scenario)
                results.append(result)
                continue

            session = Session(company_id=company_id)
            if scenario.setup == "operator_soft":
                session.messages.append(
                    Message(role=MessageRole.ASSISTANT, text=OPERATOR_SOFT_OFFER_MESSAGE)
                )
            if scenario.setup == "contact_prompt":
                session.messages.append(Message(role=MessageRole.ASSISTANT, text=CONTACT_PROMPT))
            policy_result, classification = await _policy_result_for_message(
                scenario.message,
                session,
                request,
                knowledge_base,
            )
            got_action = str(policy_result.action.value)
            got_marker = _policy_marker(policy_result, classification)
            if (
                scenario.expected_marker is None
                and scenario.expected_action in POLICY_ACTIONS
                and got_marker in SPECIAL_REASONS
            ):
                got_marker = None
            result = ScenarioResult(
                scenario.message,
                scenario.expected_action,
                scenario.expected_marker,
                got_action,
                got_marker,
                False,
            )
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
        scenario_sets = []
        # Заполняется внутри _run после загрузки FastAPI app.
        print("─" * 40)
        results = anyio.run(_run, args.company, args.real_llm, temp_dir)
        from app.main import app  # noqa: WPS433

        scenario_sets = getattr(app.state, "ai_smoke_scenario_sets", [])
        if scenario_sets:
            sources = ", ".join(
                f"{scenario_set.name} ({len(scenario_set.scenarios)})"
                for scenario_set in scenario_sets
            )
            print(f"Scenario sets: {sources}")
            print("─" * 40)
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
