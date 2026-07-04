"""Формирует cleanup queue по failed RAG sources из updated manifest."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "client-input"
    / "normalized"
    / "rosh_articles"
    / "crawl"
    / "sources_manifest.updated.json"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "review"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object in {path}")
    return payload


def _failure_type(source: dict[str, Any]) -> str:
    url = str(source.get("url") or "")
    error = str(source.get("last_error") or "")
    if "UnsupportedProtocol" in error:
        return "invalid_url_in_csv"
    if "404 Not Found" in error:
        return "not_found_404"
    if not url.startswith(("http://", "https://")):
        return "invalid_url_in_csv"
    return "crawl_error"


def _action_for_failure(failure_type: str) -> str:
    if failure_type == "invalid_url_in_csv":
        return "Найти и вставить настоящий URL страницы; сейчас в поле ссылки лежит текст/название."
    if failure_type == "not_found_404":
        return "Проверить актуальный URL на сайте или убрать источник из approved списка."
    return "Проверить URL вручную и повторить crawl с --include-failed."


def _failed_rows(manifest: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    for source in sources:
        if not isinstance(source, dict) or source.get("indexing_status") != "failed":
            continue
        failure_type = _failure_type(source)
        rows.append(
            {
                "failure_type": failure_type,
                "title": str(source.get("title") or ""),
                "url": str(source.get("url") or ""),
                "source_file": str(source.get("source_file") or ""),
                "source_row": str(source.get("source_row") or ""),
                "last_error": str(source.get("last_error") or "").replace("\n", " "),
                "recommended_action": _action_for_failure(failure_type),
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    columns = [
        "failure_type",
        "title",
        "url",
        "source_file",
        "source_row",
        "last_error",
        "recommended_action",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_md(path: Path, rows: list[dict[str, str]]) -> None:
    by_type: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_type.setdefault(row["failure_type"], []).append(row)

    lines = [
        "# Failed RAG Sources Cleanup",
        "",
        f"Total failed sources: {len(rows)}",
        "",
    ]
    for failure_type, items in sorted(by_type.items()):
        lines.extend([f"## {failure_type} ({len(items)})", ""])
        for row in items:
            lines.extend(
                [
                    f"- {row['title']}",
                    f"  source: {row['source_file']}:{row['source_row']}",
                    f"  url: {row['url']}",
                    f"  action: {row['recommended_action']}",
                    "",
                ]
            )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Сформировать cleanup queue по failed RAG sources.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = _load_manifest(args.manifest)
    rows = _failed_rows(manifest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "failed_sources.csv", rows)
    _write_md(args.output_dir / "failed_sources.md", rows)

    print(f"failed sources: {len(rows)}")
    print(f"written: {args.output_dir / 'failed_sources.csv'}")
    print(f"written: {args.output_dir / 'failed_sources.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
