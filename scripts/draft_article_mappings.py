#!/usr/bin/env python3
"""Generate pending article->service mapping drafts for human review."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from article_mapping_curation import DEFAULT_COMPANY, DEFAULT_DOCUMENTS_PATH, draft_articles


def main() -> int:
    parser = argparse.ArgumentParser(description="Draft article_service_map candidates.")
    parser.add_argument("--company", default=DEFAULT_COMPANY)
    parser.add_argument("--documents", type=Path, default=DEFAULT_DOCUMENTS_PATH)
    parser.add_argument("--clients-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--overwrite-pending", action="store_true")
    args = parser.parse_args()

    kwargs = {
        "company": args.company,
        "documents_path": args.documents,
        "limit": args.limit,
        "overwrite_pending": args.overwrite_pending,
    }
    if args.clients_dir is not None:
        kwargs["clients_dir"] = args.clients_dir
    created = asyncio.run(draft_articles(**kwargs))
    print(f"created drafts: {len(created)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
