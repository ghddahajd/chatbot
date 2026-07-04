"""Краулит staging batch статей и режет текст на chunks для будущего RAG."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_INPUT = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "articles_first_batch.json"
DEFAULT_MANIFEST = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "sources_manifest.json"
UPDATED_MANIFEST_NAME = "sources_manifest.updated.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "crawl"
DEFAULT_TIMEOUT = 20.0
DEFAULT_DELAY = 0.35
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 180
PRICE_TERM_PATTERN = re.compile(r"(?:\bцен[ауыое]\b|стоимост|руб\.?|₽)", re.IGNORECASE)


class TextExtractor(HTMLParser):
    """Минимальный extractor без новой зависимости; trafilatura можно подключить позже."""

    BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        self._parts.append(text)

    def text(self) -> str:
        raw = " ".join(self._parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s+", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return _strip_site_boilerplate(raw).strip()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_site_boilerplate(text: str) -> str:
    markers_start = [
        "Главная",
        "Услуги",
        "Блог",
        "Контакты",
    ]
    markers_end = [
        "Записаться на прием",
        "Записаться на приём",
        "Политика конфиденциальности",
        "©",
    ]
    clean = text
    for marker in markers_start:
        index = clean.find(marker)
        if 0 <= index < 500:
            clean = clean[index + len(marker):]
    for marker in markers_end:
        index = clean.find(marker)
        if index > 500:
            clean = clean[:index]
    return _strip_price_facts(clean)


def _strip_price_facts(text: str) -> str:
    """Не даёт article chunks стать источником цен; цены только из structured KB."""

    clean = re.sub(
        r"Минимальная цена\s*(?:/\s*)?от\s+\d[\d\s]*\s*(?:руб\.?|₽)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"\b(?:цена|стоимость)\s*/\s*(?:от|до|\d)[^/]{1,120}(?=\s*/|$)",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(
        r"(?:Какова цена|Стоимость лечения|Стоимость процедуры|Цена процедуры|Итоговая цена)"
        r"[^.?!]{0,900}[.?!]",
        "",
        clean,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"[^.?!]{0,240}(?:\d[\d\s]*\s*(?:руб\.?|₽))[^.?!]{0,240}[.?!]?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s{2,}", " ", clean)
    clean = re.sub(r"(?:\s*/\s*){2,}", " / ", clean)
    return clean


def _select_rosh_content(html_text: str) -> str:
    """Вырезает основной контент ROSH, чтобы не индексировать header/megamenu."""

    parts: list[str] = []
    banner_match = re.search(
        r'<div[^>]+class="[^"]*banner-slider__item-wrapper[^"]*"[^>]*>(.*?)'
        r'<div[^>]+class="[^"]*banner-slider__item-buttons[^"]*"',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if banner_match:
        parts.append(banner_match.group(1))

    for section_match in re.finditer(r"<section\b[^>]*>.*?</section>", html_text, flags=re.IGNORECASE | re.DOTALL):
        section_html = section_match.group(0)
        section_head = section_html[:500].lower()
        if "class=\"tabs" in section_head or "id=\"tseni" in section_head or "price" in section_head:
            continue
        if "записаться на прием" in section_head or "записаться на приём" in section_head:
            continue
        if len(re.sub(r"<[^>]+>", " ", section_html)) < 120:
            continue
        parts.append(section_html)

    return "\n".join(parts) if parts else html_text


def _extract_text(html_text: str) -> tuple[str, str]:
    parser = TextExtractor()
    parser.feed(_select_rosh_content(html_text))
    return parser.text(), parser.title


def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", text) if paragraph.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        if len(current) + len(paragraph) + 2 <= chunk_size:
            current = f"{current}\n\n{paragraph}"
            continue
        chunks.append(current.strip())
        tail = current[-overlap:].strip() if overlap > 0 else ""
        current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph

    if current:
        chunks.append(current.strip())

    split_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= chunk_size * 1.35:
            split_chunks.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            piece = chunk[start:start + chunk_size].strip()
            if piece:
                split_chunks.append(piece)
            start += max(1, chunk_size - overlap)
    return split_chunks


def _load_payload(path: Path) -> Any:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _load_articles(path: Path) -> list[dict[str, Any]]:
    payload = _load_payload(path)
    if not isinstance(payload, list):
        raise ValueError(f"expected list in {path}")
    return [article for article in payload if isinstance(article, dict)]


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = _load_payload(path)
    if not isinstance(payload, dict):
        raise ValueError(f"expected object in {path}")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"expected sources list in {path}")
    return payload


def _manifest_sources(manifest: dict[str, Any], *, include_failed: bool) -> list[dict[str, Any]]:
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    selected: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("approved") is not True:
            continue
        status = str(source.get("indexing_status") or "pending")
        if status == "pending" or (include_failed and status == "failed"):
            selected.append(source)
    return selected


def _manifest_status_counts(manifest: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    for source in sources:
        if not isinstance(source, dict):
            continue
        status = str(source.get("indexing_status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _apply_manifest_results(
    manifest: dict[str, Any],
    documents: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    updated = json.loads(json.dumps(manifest, ensure_ascii=False))
    docs_by_id = {str(document.get("article_id")): document for document in documents}
    pages_by_id = {str(page.get("article_id")): page for page in pages}
    failures_by_id = {str(failure.get("article_id")): failure for failure in failures}

    for source in updated.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "")
        document = docs_by_id.get(source_id)
        page = pages_by_id.get(source_id)
        failure = failures_by_id.get(source_id)
        if document and page:
            source["indexing_status"] = "indexed"
            source["content_hash"] = document.get("content_hash")
            source["text_chars"] = document.get("text_chars")
            source["chunk_count"] = page.get("chunk_count")
            source["skipped_price_chunks"] = page.get("skipped_price_chunks")
            source["last_error"] = ""
            continue
        if failure:
            source["indexing_status"] = "failed"
            source["last_error"] = f"{failure.get('error')}: {failure.get('detail')}"
    return updated


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


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


def _merge_rows(existing: list[dict[str, Any]], current: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in existing + current:
        row_key = str(row.get(key) or "")
        if not row_key:
            row_key = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if row_key not in merged:
            order.append(row_key)
        merged[row_key] = row
    return [merged[row_key] for row_key in order]


def _update_corpus_files(output_dir: Path, documents: list[dict[str, Any]], pages: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
    documents_path = output_dir / "documents.corpus.jsonl"
    pages_path = output_dir / "pages.corpus.jsonl"
    chunks_path = output_dir / "chunks.corpus.jsonl"
    _write_jsonl(documents_path, _merge_rows(_read_jsonl(documents_path), documents, "article_id"))
    _write_jsonl(pages_path, _merge_rows(_read_jsonl(pages_path), pages, "article_id"))
    _write_jsonl(chunks_path, _merge_rows(_read_jsonl(chunks_path), chunks, "chunk_id"))


def _content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fetch(client: httpx.Client, url: str) -> tuple[int, str, str]:
    fetch_url = urldefrag(url).url
    response = client.get(fetch_url)
    content_type = response.headers.get("content-type", "")
    response.raise_for_status()
    return response.status_code, content_type, response.text


def _crawl(
    articles: list[dict[str, Any]],
    *,
    timeout: float,
    delay: float,
    chunk_size: int,
    overlap: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIChatWidgetRAGStaging/1.0; +local-staging)",
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for index, article in enumerate(articles, start=1):
            url = str(article.get("url") or "").strip()
            title = _clean(article.get("title"))
            if not url:
                failures.append({"article": article, "error": "missing_url"})
                continue

            try:
                status_code, content_type, html_text = _fetch(client, url)
                text, page_title = _extract_text(html_text)
                if len(text) < 300:
                    raise ValueError(f"extracted text too short: {len(text)} chars")
                article_chunks = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
                page_payload = {
                    "article_id": article.get("id"),
                    "title": title or page_title,
                    "url": url,
                    "fetch_url": urldefrag(url).url,
                    "status_code": status_code,
                    "content_type": content_type,
                    "text_chars": len(text),
                    "chunk_count": 0,
                    "skipped_price_chunks": 0,
                    "source_file": article.get("source_file"),
                    "source_row": article.get("source_row"),
                }
                document_payload = {
                    "article_id": article.get("id"),
                    "title": page_payload["title"],
                    "url": url,
                    "fetch_url": page_payload["fetch_url"],
                    "status_code": status_code,
                    "content_type": content_type,
                    "source_content_type": article.get("content_type"),
                    "source_file": article.get("source_file"),
                    "source_row": article.get("source_row"),
                    "content_hash": _content_hash(text),
                    "text_chars": len(text),
                    "text": text,
                }
                for chunk_index, chunk in enumerate(article_chunks, start=1):
                    if PRICE_TERM_PATTERN.search(chunk):
                        page_payload["skipped_price_chunks"] += 1
                        continue
                    chunks.append(
                        {
                            "chunk_id": f"{article.get('id') or index}_{chunk_index:03d}",
                            "article_id": article.get("id"),
                            "title": page_payload["title"],
                            "url": url,
                            "content_type": article.get("content_type"),
                            "chunk_index": chunk_index,
                            "text": chunk,
                            "text_chars": len(chunk),
                            "metadata": {
                                "fetch_url": page_payload["fetch_url"],
                                "source_file": article.get("source_file"),
                                "source_row": article.get("source_row"),
                            },
                        }
                    )
                    page_payload["chunk_count"] += 1
                pages.append(page_payload)
                document_payload["chunk_count"] = page_payload["chunk_count"]
                document_payload["skipped_price_chunks"] = page_payload["skipped_price_chunks"]
                documents.append(document_payload)
            except Exception as error:  # noqa: BLE001 - staging report должен сохранить любую ошибку.
                failures.append(
                    {
                        "article_id": article.get("id"),
                        "title": title,
                        "url": url,
                        "error": type(error).__name__,
                        "detail": str(error),
                    }
                )

            if delay > 0 and index < len(articles):
                time.sleep(delay)

    return documents, pages, chunks, failures


def _report(
    articles: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> str:
    total_chars = sum(int(page.get("text_chars") or 0) for page in pages)
    skipped_price_chunks = sum(int(page.get("skipped_price_chunks") or 0) for page in pages)
    lines = [
        "# ROSH Articles Crawl Report",
        "",
        "## Summary",
        f"- input articles: {len(articles)}",
        f"- document snapshots: {len(documents)}",
        f"- fetched pages: {len(pages)}",
        f"- failed pages: {len(failures)}",
        f"- chunks: {len(chunks)}",
        f"- skipped price chunks: {skipped_price_chunks}",
        f"- extracted chars: {total_chars}",
        "",
        "## Pages",
        *[
            f"- {page.get('chunk_count')} chunks"
            f" / {page.get('skipped_price_chunks', 0)} price-skipped"
            f" / {page.get('text_chars')} chars — {page.get('title')} — {page.get('url')}"
            for page in pages
        ],
    ]
    if failures:
        lines.extend(
            [
                "",
                "## Failures",
                *[
                    f"- {failure.get('error')}: {failure.get('title')} — {failure.get('url')} — {failure.get('detail')}"
                    for failure in failures
                ],
            ]
        )
    lines.extend(
        [
            "",
            "## Notes",
            "- This is staging only. Chunks are not connected to runtime answers.",
            "- `documents.jsonl` stores cleaned text snapshots with `content_hash` for audit/diff.",
            "- Prices/services must still come from structured KB, not from article chunks.",
            "- Review extracted text quality before embeddings/pgvector ingestion.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Краулить batch статей и нарезать chunks для staging RAG.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Взять output-dir/sources_manifest.updated.json, если он есть, иначе --manifest или дефолтный manifest.",
    )
    parser.add_argument("--include-failed", action="store_true", help="Повторить источники manifest со статусом failed.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--limit", type=int, default=0, help="0 = все статьи из batch")
    parser.add_argument("--batch-size", type=int, default=0, help="Размер следующей пачки pending-источников из manifest.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    updated_manifest_path = args.output_dir / UPDATED_MANIFEST_NAME
    manifest_path = args.manifest
    if args.resume:
        manifest_path = updated_manifest_path if updated_manifest_path.exists() else (manifest_path or DEFAULT_MANIFEST)

    manifest = _load_manifest(manifest_path) if manifest_path else None
    articles = _manifest_sources(manifest, include_failed=args.include_failed) if manifest else _load_articles(args.input)
    batch_size = args.batch_size if args.batch_size > 0 else args.limit
    if batch_size > 0:
        articles = articles[:batch_size]
    elif args.limit > 0:
        articles = articles[:args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    documents, pages, chunks, failures = _crawl(
        articles,
        timeout=args.timeout,
        delay=args.delay,
        chunk_size=args.chunk_size,
        overlap=args.chunk_overlap,
    )

    _write_jsonl(args.output_dir / "documents.jsonl", documents)
    _write_jsonl(args.output_dir / "pages.jsonl", pages)
    _write_jsonl(args.output_dir / "chunks.jsonl", chunks)
    _write_json(args.output_dir / "failures.json", failures)
    if manifest is not None:
        updated_manifest = _apply_manifest_results(manifest, documents, pages, failures)
        _write_json(updated_manifest_path, updated_manifest)
        _update_corpus_files(args.output_dir, documents, pages, chunks)
    (args.output_dir / "crawl_report.md").write_text(
        _report(articles, documents, pages, chunks, failures),
        encoding="utf-8",
    )

    print(f"written: {args.output_dir / 'documents.jsonl'}")
    print(f"written: {args.output_dir / 'pages.jsonl'}")
    print(f"written: {args.output_dir / 'chunks.jsonl'}")
    print(f"written: {args.output_dir / 'failures.json'}")
    if manifest is not None:
        print(f"written: {updated_manifest_path}")
        print(f"updated corpus: {args.output_dir / 'documents.corpus.jsonl'}")
        print(f"updated corpus: {args.output_dir / 'chunks.corpus.jsonl'}")
        print(f"manifest_status: {_manifest_status_counts(updated_manifest)}")
    print(f"written: {args.output_dir / 'crawl_report.md'}")
    print(f"documents: {len(documents)}; pages: {len(pages)}; chunks: {len(chunks)}; failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
