#!/usr/bin/env python3
"""Interactive review for article mapping drafts."""

from __future__ import annotations

import argparse
from pathlib import Path

from article_mapping_curation import (
    DEFAULT_COMPANY,
    NEEDS_MEDICAL_REVIEW_FILE,
    approve_draft,
    article_map_path,
    drafts_dir,
    escalate_draft,
    load_draft,
    pending_draft_files,
    reject_draft,
    review_summary,
)


def _print_candidate(index: int, candidate: dict) -> None:
    relation = candidate.get("relation") or ""
    service_id = candidate.get("service_id") or ""
    confidence = candidate.get("confidence") or 0
    evidence = candidate.get("evidence") or ""
    reason = candidate.get("reason") or ""
    marker = "[approve]" if relation == "topic_related" else "[context]"
    print(f"  {index}. {marker} {service_id} | {relation} | confidence={confidence}")
    if evidence:
        print(f"     evidence: {evidence}")
    if reason:
        print(f"     reason: {reason}")


def _parse_selection(value: str, candidates: list[dict]) -> list[str] | None:
    parts = value.split(maxsplit=1)
    if len(parts) == 1:
        return None
    selected: list[str] = []
    for raw_index in parts[1].split(","):
        raw_index = raw_index.strip()
        if not raw_index.isdigit():
            continue
        index = int(raw_index) - 1
        if 0 <= index < len(candidates):
            selected.append(str(candidates[index].get("service_id") or ""))
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Review pending article mapping drafts.")
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--clients-dir", type=Path, default=None)
    parser.add_argument("--risk", choices=["green", "yellow", "red"], default=None)
    args = parser.parse_args()

    clients_dir = args.clients_dir
    root = drafts_dir(args.company, clients_dir) if clients_dir else drafts_dir(args.company)
    mapping_path = article_map_path(args.company, clients_dir) if clients_dir else article_map_path(args.company)
    review_path = root.parent / NEEDS_MEDICAL_REVIEW_FILE
    files = pending_draft_files(root, risk=args.risk)

    for path in files:
        draft = load_draft(path)
        candidates = [c for c in draft.get("service_candidates") or [] if isinstance(c, dict)]
        print("\n" + "=" * 80)
        print(f"{draft.get('title') or path.name}")
        print(f"url: {draft.get('url') or ''}")
        print(f"risk: {draft.get('risk_score') or ''} | flags: {', '.join(draft.get('risk_flags') or []) or '-'}")
        print(f"trigger_phrases: {', '.join(draft.get('trigger_phrases') or []) or '-'}")
        print(f"note: {draft.get('reviewer_note') or '-'}")
        for index, candidate in enumerate(candidates, start=1):
            _print_candidate(index, candidate)

        while True:
            action = input("[a]pprove [r]eject [e]scalate [s]kip [q]uit > ").strip().lower()
            if action == "q":
                return 0
            if action.startswith("a"):
                try:
                    approve_draft(path, mapping_path, _parse_selection(action, candidates))
                except ValueError as error:
                    print(f"cannot approve: {error}")
                    continue
                print("approved")
                break
            if action == "r":
                reject_draft(path)
                print("rejected")
                break
            if action == "e":
                escalate_draft(path, review_path)
                print("escalated")
                break
            if action == "s":
                print("skipped")
                break

    summary = review_summary(root)
    print("\nReview summary")
    for risk, counters in summary.items():
        print(f"{risk.upper()}: done={counters['done']} pending={counters['pending']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
