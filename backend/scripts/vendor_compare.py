"""Side-by-side comparison against vendor dialog examples.

The script intentionally runs the normal /api/chat/message flow, not just policy
classification, because the useful signal here is the final text shown to a user.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
EVALS_DIR = BACKEND_DIR / "evals"
DEFAULT_EVAL = EVALS_DIR / "vendor_comparison.jsonl"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tasks" / "vendor_compare_runs"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class VendorCase:
    id: str
    company: str
    message: str
    history: list[dict[str, str]]
    vendor_answer: str
    client_note: str
    expected_action: str | None


@dataclass(frozen=True)
class CompareResult:
    case: VendorCase
    status_code: int
    action: str
    answer: str
    quick_actions: list[str]
    lead_created: bool
    verdict: str


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
    os.environ["LLM_USE_STRUCTURED_CLASSIFIER"] = "true"
    os.environ["CHAT_RATE_LIMIT_ENABLED"] = "false"
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


def _resolve_eval_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() and not path.exists():
        path = EVALS_DIR / value
    return path


def _load_cases(path: Path, company_id: str) -> list[VendorCase]:
    cases: list[VendorCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        case_company = str(payload.get("company") or company_id)
        if case_company != company_id:
            continue
        raw_history = payload.get("history") or []
        if not isinstance(raw_history, list):
            raise ValueError(f"{path}:{line_number}: history должен быть list")
        history = [
            {
                "role": str(item.get("role") or "user"),
                "text": str(item.get("text") or ""),
            }
            for item in raw_history
            if isinstance(item, dict)
        ]
        cases.append(
            VendorCase(
                id=str(payload.get("id") or f"{path.stem}:{line_number}"),
                company=case_company,
                message=str(payload.get("message") or ""),
                history=history,
                vendor_answer=str(payload.get("vendor_answer") or ""),
                client_note=str(payload.get("client_note") or ""),
                expected_action=str(payload.get("expected_action") or "").strip() or None,
            )
        )
    return cases


async def _seed_history(app: Any, company_id: str, history: list[dict[str, str]]) -> str:
    from app.models import MessageRole  # noqa: WPS433

    session_store = app.state.session_store
    session = await session_store.get_or_create(None, company_id)
    for item in history:
        role = MessageRole.ASSISTANT if item["role"] == "assistant" else MessageRole.USER
        if item["text"].strip():
            await session_store.append_message(session.session_id, role, item["text"])
    return str(session.session_id)


def _quick_action_labels(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for action in payload.get("quick_actions") or []:
        if isinstance(action, dict):
            label = str(action.get("label") or "").strip()
            if label:
                labels.append(label)
    return labels


def _verdict(case: VendorCase, action: str, answer: str) -> str:
    normalized = answer.lower()
    if case.expected_action is None:
        return "eyeball"
    if action == case.expected_action:
        if action == "transfer_operator":
            return "наш безопаснее"
        if action == "clarify":
            return "наш осторожнее"
        return "ok"
    if case.expected_action == "clarify" and action == "answer":
        suspicious_terms = {
            "forever young",
            "диспорт",
            "ботулекс",
            "лантокс",
            "омс",
            "скорая",
            "остеопат",
            "левашов",
            "келост",
            "колост",
        }
        if any(term in normalized for term in suspicious_terms):
            return "наш хуже: возможно подтвердил факт"
    return f"проверить: expected {case.expected_action}, got {action}"


def _run_case(client: Any, app: Any, company_id: str, case: VendorCase) -> CompareResult:
    import anyio

    session_id = anyio.run(_seed_history, app, company_id, case.history)
    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": case.message},
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"answer": response.text}
    action = str(payload.get("action") or f"http_{response.status_code}")
    answer = str(payload.get("answer") or "")
    quick_actions = _quick_action_labels(payload)
    if quick_actions:
        answer = f"{answer}\nКнопки: {', '.join(quick_actions)}"
    return CompareResult(
        case=case,
        status_code=response.status_code,
        action=action,
        answer=answer,
        quick_actions=quick_actions,
        lead_created=bool(payload.get("lead_created")),
        verdict=_verdict(case, action, answer),
    )


def _md_cell(value: str) -> str:
    text = (value or "").replace("\n", "<br>")
    text = text.replace("|", "\\|")
    return text.strip() or "—"


def _render_markdown(company_id: str, results: list[CompareResult]) -> str:
    lines = [
        f"# Vendor Comparison — {company_id}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "| # | ID | Вопрос | Вендор | Наш | Verdict | Клиент |",
        "|---:|---|---|---|---|---|---|",
    ]
    for index, result in enumerate(results, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    _md_cell(result.case.id),
                    _md_cell(result.case.message),
                    _md_cell(result.case.vendor_answer),
                    _md_cell(f"[{result.action}] {result.answer}"),
                    _md_cell(result.verdict),
                    _md_cell(result.case.client_note),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _print_result(result: CompareResult) -> None:
    print("─" * 80)
    print(f"{result.case.id} | action={result.action} | verdict={result.verdict}")
    print(f"U: {result.case.message}")
    print(f"НАШ: {result.answer}")
    print(f"VENDOR: {result.case.vendor_answer or '—'}")
    if result.case.client_note:
        print(f"CLIENT: {result.case.client_note}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Side-by-side vendor dialog comparison.")
    parser.add_argument("--company", default="rosh_import_demo")
    parser.add_argument("--eval", default=str(DEFAULT_EVAL), help="JSONL path or name under backend/evals")
    parser.add_argument("--output", default="", help="Markdown output path. Defaults to tasks/vendor_compare_runs/.")
    parser.add_argument("--limit", type=int, default=0, help="Limit cases for quick checks.")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--llm-provider", default="", help="override LLM_PROVIDER for this run")
    parser.add_argument("--llm-base-url", default="", help="override LLM_BASE_URL for this run")
    parser.add_argument("--llm-model", default="", help="override LLM_MODEL for this run")
    parser.add_argument("--llm-api-key", default="", help="override LLM_API_KEY for this run")
    return parser.parse_args()


def _output_path(value: str, company_id: str) -> Path:
    if value:
        return Path(value)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"vendor_compare_{company_id}_{stamp}.md"


def main() -> int:
    args = parse_args()
    eval_path = _resolve_eval_path(args.eval)
    cases = _load_cases(eval_path, args.company)
    if args.limit > 0:
        cases = cases[: args.limit]

    with tempfile.TemporaryDirectory(prefix="chatbot-vendor-compare-") as temp:
        temp_dir = Path(temp)
        _configure_env(args.company, args.real_llm, temp_dir)
        _apply_llm_overrides(args)

        from fastapi.testclient import TestClient  # noqa: WPS433
        from app.config import get_settings  # noqa: WPS433

        get_settings.cache_clear()

        from app.main import app  # noqa: WPS433

        print(f"Vendor Comparison — {args.company}")
        print(f"eval: {eval_path}")
        print(f"provider: {'real' if args.real_llm else 'mock'}")
        print("═" * 80)
        results: list[CompareResult] = []
        with TestClient(app) as client:
            app.state.knowledge_base_resolver.get(args.company, fallback=False)
            for case in cases:
                result = _run_case(client, app, args.company, case)
                results.append(result)
                _print_result(result)

        output_path = _output_path(args.output, args.company)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_render_markdown(args.company, results), encoding="utf-8")

        better = sum(1 for result in results if result.verdict in {"наш безопаснее", "наш осторожнее"})
        worse = sum(1 for result in results if "хуже" in result.verdict or result.verdict.startswith("проверить"))
        print("═" * 80)
        print(f"cases: {len(results)}")
        print(f"better_or_safer: {better}")
        print(f"needs_review: {worse}")
        print(f"saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
