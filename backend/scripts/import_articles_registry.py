"""Импортирует реестр статей клиента в staging без публикации в runtime KB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_INPUTS = (
    REPO_ROOT / "client-input" / "excel" / "ROSH сводная по статьям - Новые тексты.csv",
    REPO_ROOT / "client-input" / "excel" / "ROSH сводная по статьям - Доработка.csv",
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles"
APPROVED_STATUS_MARKERS = {
    "размещена",
    "размещено",
    "опубликована",
    "опубликовано",
    "правки учтены",
}
FIRST_BATCH_LIMIT = 20


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\ufeff", "")).strip()


def _normalize_status(value: str) -> str:
    return _clean(value).lower().replace("ё", "е")


def _slugify(value: str) -> str:
    value = _clean(value).lower().replace("ё", "е")
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    value = "".join(translit.get(char, char) for char in value)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_") or "article"


def _article_id(title: str, url: str) -> str:
    digest = hashlib.sha1(f"{title}|{url}".encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(title)[:56].strip('_')}_{digest}"


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return [[_clean(cell) for cell in row] for row in csv.reader(file)]


def _header_from_row(row: list[str]) -> list[str]:
    headers = [_clean(value) for value in row]
    return [header or f"column_{index + 1}" for index, header in enumerate(headers)]


def _row_values(headers: list[str], row: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, header in enumerate(headers):
        values[header] = _clean(row[index]) if index < len(row) else ""
    extra = [_clean(cell) for cell in row[len(headers):] if _clean(cell)]
    if extra:
        values["_extra"] = " | ".join(extra)
    return values


def _looks_like_header(row: list[str]) -> bool:
    normalized = [_clean(cell).lower() for cell in row]
    return "тема" in normalized and "статус" in normalized and any("ссылка" in cell for cell in normalized)


def _is_section_row(row: list[str]) -> bool:
    non_empty = [_clean(cell) for cell in row if _clean(cell)]
    if not non_empty:
        return False
    if _looks_like_header(row):
        return False
    first = non_empty[0].lower()
    return len(non_empty) == 1 or first in {
        "март",
        "апрель",
        "май",
        "июнь",
        "июль",
        "август",
        "сентябрь",
        "октябрь",
        "ноябрь",
        "декабрь",
        "январь",
        "февраль",
    }


def _value_by_alias(values: dict[str, str], aliases: tuple[str, ...]) -> str:
    lowered = {key.lower(): value for key, value in values.items()}
    for alias in aliases:
        if alias.lower() in lowered and lowered[alias.lower()]:
            return lowered[alias.lower()]
    return ""


def _content_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/blog/" in path:
        return "blog"
    if "/services/" in path:
        return "service_page"
    if path:
        return "page"
    return "unknown"


def _is_approved(status: str) -> bool:
    normalized = _normalize_status(status)
    return any(marker in normalized for marker in APPROVED_STATUS_MARKERS)


def _parse_source(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _read_csv_rows(path)
    headers: list[str] = []
    section = ""
    raw_rows: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []

    for row_number, row in enumerate(rows, start=1):
        if not any(_clean(cell) for cell in row):
            continue

        if _looks_like_header(row):
            headers = _header_from_row(row)
            possible_section = _clean(row[0])
            if possible_section and possible_section.lower() not in {"аа", "сентябрь"}:
                section = possible_section
            raw_rows.append(
                {
                    "source_file": path.name,
                    "row_number": row_number,
                    "row_type": "header",
                    "section": section,
                    "headers": headers,
                    "raw_values": row,
                    "values": _row_values(headers, row),
                }
            )
            continue

        if not headers:
            raw_rows.append(
                {
                    "source_file": path.name,
                    "row_number": row_number,
                    "row_type": "unknown_before_header",
                    "section": section,
                    "headers": [],
                    "raw_values": row,
                    "values": {},
                }
            )
            continue

        if _is_section_row(row):
            section = _clean(row[0])
            row_type = "section"
        else:
            row_type = "article"

        values = _row_values(headers, row)
        raw_rows.append(
            {
                "source_file": path.name,
                "row_number": row_number,
                "row_type": row_type,
                "section": section,
                "headers": headers,
                "raw_values": row,
                "values": values,
            }
        )

        if row_type != "article":
            continue

        title = _value_by_alias(values, ("Тема", "Статья", "Новый текст", "Текст ДО"))
        status = _value_by_alias(values, ("Статус",))
        url = _value_by_alias(values, ("Ссылка на сайте",))
        if not title and not url:
            continue

        articles.append(
            {
                "id": _article_id(title or url, url),
                "title": title,
                "status": status,
                "approved_for_rag": _is_approved(status),
                "url": url,
                "content_type": _content_type(url),
                "section": section,
                "deadline": _value_by_alias(values, ("Дедлайн",)),
                "task_file": _value_by_alias(values, ("ТЗ",)),
                "source_file": path.name,
                "source_row": row_number,
                "metadata": {
                    "raw": values,
                    "raw_values": row,
                    "headers": headers,
                },
            }
        )

    return raw_rows, articles


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _dedupe_by_url(articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    seen: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for article in articles:
        url = str(article.get("url") or "").strip()
        if not url:
            continue
        if url in seen:
            duplicate = dict(article)
            duplicate["duplicate_of"] = seen[url].get("id")
            duplicates.append(duplicate)
            continue
        seen[url] = article
    return list(seen.values()), duplicates


def _first_batch(articles: list[dict[str, Any]], limit: int = FIRST_BATCH_LIMIT) -> list[dict[str, Any]]:
    service_pages = [article for article in articles if article.get("content_type") == "service_page"]
    blog_pages = [article for article in articles if article.get("content_type") == "blog"]
    other_pages = [article for article in articles if article.get("content_type") not in {"service_page", "blog"}]
    ordered = service_pages[:10] + blog_pages[:8] + other_pages
    return ordered[:limit]


def _sources_manifest(articles: list[dict[str, Any]], company_id: str = "rosh_import_demo") -> dict[str, Any]:
    return {
        "company_id": company_id,
        "source": "articles_registry",
        "policy": {
            "prices_from_rag": False,
            "requires_human_review": True,
        },
        "sources": [
            {
                "id": article.get("id"),
                "title": article.get("title"),
                "url": article.get("url"),
                "content_type": article.get("content_type"),
                "approved": True,
                "indexing_status": "pending",
                "source_file": article.get("source_file"),
                "source_row": article.get("source_row"),
            }
            for article in articles
        ],
    }


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _manifest_yaml(manifest: dict[str, Any]) -> str:
    lines = [
        f"company_id: {_yaml_scalar(manifest.get('company_id'))}",
        f"source: {_yaml_scalar(manifest.get('source'))}",
        "policy:",
    ]
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    for key, value in policy.items():
        lines.append(f"  {key}: {_yaml_scalar(value)}")
    lines.append("sources:")
    for source in manifest.get("sources", []):
        if not isinstance(source, dict):
            continue
        lines.append(f"  - id: {_yaml_scalar(source.get('id'))}")
        lines.append(f"    title: {_yaml_scalar(source.get('title'))}")
        lines.append(f"    url: {_yaml_scalar(source.get('url'))}")
        lines.append(f"    content_type: {_yaml_scalar(source.get('content_type'))}")
        lines.append(f"    approved: {_yaml_scalar(source.get('approved'))}")
        lines.append(f"    indexing_status: {_yaml_scalar(source.get('indexing_status'))}")
        lines.append(f"    source_file: {_yaml_scalar(source.get('source_file'))}")
        lines.append(f"    source_row: {_yaml_scalar(source.get('source_row'))}")
    return "\n".join(lines) + "\n"


def _review_rows(articles: list[dict[str, Any]], duplicates: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for article in articles:
        if not article.get("url"):
            rows.append(
                {
                    "issue": "missing_url",
                    "source_file": str(article.get("source_file") or ""),
                    "source_row": str(article.get("source_row") or ""),
                    "title": str(article.get("title") or ""),
                    "status": str(article.get("status") or ""),
                    "url": "",
                    "note": "Нет URL, нельзя краулить для RAG до уточнения.",
                }
            )
        if not article.get("approved_for_rag"):
            rows.append(
                {
                    "issue": "not_approved",
                    "source_file": str(article.get("source_file") or ""),
                    "source_row": str(article.get("source_row") or ""),
                    "title": str(article.get("title") or ""),
                    "status": str(article.get("status") or ""),
                    "url": str(article.get("url") or ""),
                    "note": "Статус не считается опубликованным/одобренным.",
                }
            )
    for article in duplicates:
        rows.append(
            {
                "issue": "duplicate_url",
                "source_file": str(article.get("source_file") or ""),
                "source_row": str(article.get("source_row") or ""),
                "title": str(article.get("title") or ""),
                "status": str(article.get("status") or ""),
                "url": str(article.get("url") or ""),
                "note": f"Дубликат URL, основной article_id: {article.get('duplicate_of')}",
            }
        )
    return rows


def _write_review_csv(path: Path, rows: list[dict[str, str]]) -> None:
    columns = ["issue", "source_file", "source_row", "title", "status", "url", "note"]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _report(
    articles: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    ready: list[dict[str, Any]],
    ready_unique: list[dict[str, Any]],
    first_batch: list[dict[str, Any]],
    review_rows: list[dict[str, str]],
    manifest: dict[str, Any],
) -> str:
    status_counts = Counter(str(article.get("status") or "без статуса") for article in articles)
    type_counts = Counter(str(article.get("content_type") or "unknown") for article in articles)
    source_counts = Counter(str(article.get("source_file") or "unknown") for article in articles)
    duplicate_urls = [
        url for url, count in Counter(str(article.get("url") or "") for article in articles if article.get("url")).items()
        if count > 1
    ]
    missing_url = [article for article in articles if not article.get("url")]
    lines = [
        "# ROSH Articles Import Report",
        "",
        "## Summary",
        f"- raw rows saved: {len(raw_rows)}",
        f"- article rows parsed: {len(articles)}",
        f"- ready for RAG: {len(ready)}",
        f"- ready unique URLs: {len(ready_unique)}",
        f"- first crawl batch: {len(first_batch)}",
        f"- manifest sources: {len(manifest.get('sources') or [])}",
        f"- missing URL: {len(missing_url)}",
        f"- duplicate URLs: {len(duplicate_urls)}",
        f"- review rows: {len(review_rows)}",
        "",
        "## By source",
        *[f"- {source}: {count}" for source, count in sorted(source_counts.items())],
        "",
        "## By status",
        *[f"- {status}: {count}" for status, count in sorted(status_counts.items())],
        "",
        "## By content type",
        *[f"- {content_type}: {count}" for content_type, count in sorted(type_counts.items())],
        "",
        "## Notes",
        "- This is staging only. Runtime KB and chat answers are not changed.",
        "- `raw_article_rows.jsonl` preserves source rows, headers and all original columns.",
        "- `articles_ready_for_rag.json` includes approved rows with a URL and should be crawled only after review.",
        "- `articles_ready_unique.json` removes duplicate URLs for crawler input, but does not delete original rows.",
        "- `articles_first_batch.json` is a small smoke batch for the first RAG ingestion test.",
        "- `sources_manifest.json/yaml` is the reviewable approved-source manifest for ingestion.",
        "- `articles_review.csv` is the human cleanup queue for missing URLs, non-approved rows and duplicates.",
    ]
    if duplicate_urls:
        lines.extend(["", "## Duplicate URLs", *[f"- {url}" for url in duplicate_urls[:50]]])
    if missing_url:
        lines.extend(
            [
                "",
                "## Missing URL Samples",
                *[
                    f"- {article.get('source_file')}:{article.get('source_row')} — {article.get('title') or '(no title)'}"
                    for article in missing_url[:50]
                ],
            ]
        )
    if first_batch:
        lines.extend(
            [
                "",
                "## First Crawl Batch",
                *[
                    f"- {article.get('content_type')} — {article.get('title')} — {article.get('url')}"
                    for article in first_batch
                ],
            ]
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импортировать реестр статей в staging.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--input", type=Path, action="append", dest="inputs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = args.inputs or list(DEFAULT_INPUTS)
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_rows: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    for input_path in inputs:
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        if input_path.suffix.lower() != ".csv":
            raise ValueError(f"only CSV inputs are supported for this staging step: {input_path}")
        source_raw_rows, source_articles = _parse_source(input_path)
        raw_rows.extend(source_raw_rows)
        articles.extend(source_articles)

    ready = [
        article
        for article in articles
        if article.get("approved_for_rag") and article.get("url")
    ]
    ready_unique, duplicates = _dedupe_by_url(ready)
    first_batch = _first_batch(ready_unique)
    review_rows = _review_rows(articles, duplicates)
    manifest = _sources_manifest(ready_unique)

    _write_jsonl(output_dir / "raw_article_rows.jsonl", raw_rows)
    _write_json(output_dir / "articles_registry.json", articles)
    _write_json(output_dir / "articles_ready_for_rag.json", ready)
    _write_json(output_dir / "articles_ready_unique.json", ready_unique)
    _write_json(output_dir / "articles_first_batch.json", first_batch)
    _write_json(output_dir / "sources_manifest.json", manifest)
    (output_dir / "sources_manifest.yaml").write_text(_manifest_yaml(manifest), encoding="utf-8")
    _write_review_csv(output_dir / "articles_review.csv", review_rows)
    (output_dir / "articles_import_report.md").write_text(
        _report(articles, raw_rows, ready, ready_unique, first_batch, review_rows, manifest),
        encoding="utf-8",
    )

    print(f"written: {output_dir / 'raw_article_rows.jsonl'}")
    print(f"written: {output_dir / 'articles_registry.json'}")
    print(f"written: {output_dir / 'articles_ready_for_rag.json'}")
    print(f"written: {output_dir / 'articles_ready_unique.json'}")
    print(f"written: {output_dir / 'articles_first_batch.json'}")
    print(f"written: {output_dir / 'sources_manifest.json'}")
    print(f"written: {output_dir / 'sources_manifest.yaml'}")
    print(f"written: {output_dir / 'articles_review.csv'}")
    print(f"written: {output_dir / 'articles_import_report.md'}")
    print(f"articles: {len(articles)}; ready_for_rag: {len(ready)}; ready_unique: {len(ready_unique)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
