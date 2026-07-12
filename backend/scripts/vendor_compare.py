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
DEMO_GATE_OUTPUT_DIR = REPO_ROOT / "tasks" / "demo_gate_runs"
DEMO_GATE_EVALS = (
    EVALS_DIR / "vendor_comparison.jsonl",
    EVALS_DIR / "client_bug_regressions.jsonl",
    EVALS_DIR / "vendor_reference_regressions.jsonl",
)
FULL_EVAL = EVALS_DIR / "manual_test_scenarios.jsonl"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class VendorCase:
    source: str
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
                source=path.name,
                company=case_company,
                message=str(payload.get("message") or ""),
                history=history,
                vendor_answer=str(payload.get("vendor_answer") or ""),
                client_note=str(payload.get("client_note") or ""),
                expected_action=str(payload.get("expected_action") or "").strip() or None,
            )
        )
    return cases


def _post_chat_message(client: Any, company_id: str, session_id: str | None, message: str) -> dict[str, Any]:
    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": message},
    )
    try:
        payload = response.json()
    except Exception:
        payload = {"answer": response.text}
    payload["_status_code"] = response.status_code
    return payload


def _replay_history(client: Any, company_id: str, history: list[dict[str, str]]) -> str | None:
    session_id: str | None = None
    for item in history:
        if item["role"] != "user":
            continue
        text = item["text"].strip()
        if not text:
            continue
        payload = _post_chat_message(client, company_id, session_id, text)
        session_id = str(payload.get("session_id") or session_id or "")
    return session_id or None


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
    del app
    session_id = _replay_history(client, company_id, case.history)
    payload = _post_chat_message(client, company_id, session_id, case.message)
    status_code = int(payload.get("_status_code") or 0)
    action = str(payload.get("action") or f"http_{status_code}")
    answer = str(payload.get("answer") or "")
    quick_actions = _quick_action_labels(payload)
    if quick_actions:
        answer = f"{answer}\nКнопки: {', '.join(quick_actions)}"
    return CompareResult(
        case=case,
        status_code=status_code,
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
        "| # | Файл | ID | Компания | Вопрос | Вендор | Наш | Ожидаемо | Verdict | Заметка |",
        "|---:|---|---|---|---|---|---|---|---|---|",
    ]
    for index, result in enumerate(results, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    _md_cell(result.case.source),
                    _md_cell(result.case.id),
                    _md_cell(result.case.company),
                    _md_cell(result.case.message),
                    _md_cell(result.case.vendor_answer),
                    _md_cell(f"[{result.action}] {result.answer}"),
                    _md_cell(result.case.expected_action or "eyeball"),
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
    print(f"{result.case.source}:{result.case.id} | action={result.action} | verdict={result.verdict}")
    print(f"U: {result.case.message}")
    print(f"НАШ: {result.answer}")
    print(f"VENDOR: {result.case.vendor_answer or '—'}")
    if result.case.client_note:
        print(f"CLIENT: {result.case.client_note}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Side-by-side vendor dialog comparison.")
    parser.add_argument("--company", default="rosh_import_demo")
    parser.add_argument(
        "--eval",
        action="append",
        default=None,
        help="JSONL path or name under backend/evals. Can be passed multiple times.",
    )
    parser.add_argument("--full", action="store_true", help="Add manual_test_scenarios.jsonl to the eval set.")
    parser.add_argument("--demo-gate", action="store_true", help="Use the default demo gate eval pack.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any result needs review.")
    parser.add_argument("--output", default="", help="Markdown output path. Defaults to tasks/vendor_compare_runs/.")
    parser.add_argument("--limit", type=int, default=0, help="Limit cases for quick checks.")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--llm-provider", default="", help="override LLM_PROVIDER for this run")
    parser.add_argument("--llm-base-url", default="", help="override LLM_BASE_URL for this run")
    parser.add_argument("--llm-model", default="", help="override LLM_MODEL for this run")
    parser.add_argument("--llm-api-key", default="", help="override LLM_API_KEY for this run")
    return parser.parse_args()


def _eval_paths(args: argparse.Namespace) -> list[Path]:
    if args.eval:
        paths = [_resolve_eval_path(value) for value in args.eval]
    elif args.demo_gate:
        paths = list(DEMO_GATE_EVALS)
    else:
        paths = [DEFAULT_EVAL]
    if args.full:
        paths.append(FULL_EVAL)
    return paths


def _output_path(value: str, company_id: str, *, demo_gate: bool = False) -> Path:
    if value:
        return Path(value)
    output_dir = DEMO_GATE_OUTPUT_DIR if demo_gate else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "demo_gate" if demo_gate else "vendor_compare"
    return output_dir / f"{prefix}_{company_id}_{stamp}.md"


def main() -> int:
    args = parse_args()
    eval_paths = _eval_paths(args)
    cases: list[VendorCase] = []
    for eval_path in eval_paths:
        cases.extend(_load_cases(eval_path, args.company))
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

        title = "Demo Gate" if args.demo_gate else "Vendor Comparison"
        print(f"{title} — {args.company}")
        print("evals:")
        for eval_path in eval_paths:
            print(f"  - {eval_path}")
        print(f"provider: {'real' if args.real_llm else 'mock'}")
        print("═" * 80)
        results: list[CompareResult] = []
        with TestClient(app) as client:
            app.state.knowledge_base_resolver.get(args.company, fallback=False)
            for case in cases:
                result = _run_case(client, app, args.company, case)
                results.append(result)
                _print_result(result)

        output_path = _output_path(args.output, args.company, demo_gate=args.demo_gate)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_render_markdown(args.company, results), encoding="utf-8")

        better = sum(1 for result in results if result.verdict in {"наш безопаснее", "наш осторожнее"})
        eyeball = sum(1 for result in results if result.verdict == "eyeball")
        needs_review = sum(
            1
            for result in results
            if "хуже" in result.verdict or result.verdict.startswith("проверить")
        )
        ok = sum(1 for result in results if result.verdict == "ok")
        print("═" * 80)
        print(f"cases: {len(results)}")
        print(f"ok: {ok}")
        print(f"better_or_safer: {better}")
        print(f"eyeball: {eyeball}")
        print(f"needs_review: {needs_review}")
        print(f"saved: {output_path}")
    return 1 if args.strict and needs_review else 0


if __name__ == "__main__":
    raise SystemExit(main())
