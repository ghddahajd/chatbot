#!/usr/bin/env python3
"""Report/validate article mapping drafts before manual curation batches."""

from __future__ import annotations

import argparse
from pathlib import Path

from article_mapping_curation import (
    DEFAULT_COMPANY,
    batch_files,
    draft_report,
    drafts_dir,
    load_services_for_prompt,
    validate_draft_files,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report article mapping draft status.")
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--clients-dir", type=Path, default=None)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--batch", type=int, default=0, help="Print next N pending files.")
    parser.add_argument("--bucket", default=None, help="Filter batch by bucket, e.g. green_empty.")
    parser.add_argument("--unreviewed-only", action="store_true", help="Skip drafts marked 'Codex reviewed'.")
    args = parser.parse_args()

    clients_dir = args.clients_dir
    root = drafts_dir(args.company, clients_dir) if clients_dir else drafts_dir(args.company)
    report = draft_report(root)
    print(f"drafts_dir: {root}")
    print(f"total: {report['total']}")
    print(f"codex_reviewed_pending: {report['codex_reviewed_pending']}")
    for bucket, count in sorted(report["buckets"].items()):
        print(f"{bucket}: {count}")
    if report["invalid"]:
        print("invalid_yaml:")
        for item in report["invalid"]:
            print(f"- {item['file']}: {item['error']}")

    if args.validate:
        services = load_services_for_prompt(args.company, clients_dir) if clients_dir else load_services_for_prompt(args.company)
        known_ids = {str(service.get("id") or "") for service in services if service.get("id")}
        issues = validate_draft_files(root, known_ids)
        print(f"validation_issues: {len(issues)}")
        for issue in issues[:50]:
            print(f"- {issue['file']}: {issue['error']}")

    if args.batch > 0:
        print(
            f"next_batch limit={args.batch} bucket={args.bucket or '*'} "
            f"unreviewed_only={args.unreviewed_only}:"
        )
        for path in batch_files(
            root,
            limit=args.batch,
            bucket=args.bucket,
            unreviewed_only=args.unreviewed_only,
        ):
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
