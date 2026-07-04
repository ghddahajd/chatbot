"""Готовит staging JSONL payload для будущей загрузки RAG corpus в Postgres/pgvector."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_CRAWL_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "crawl"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "pgvector_payload"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: expected object")
        rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _hash_text(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _document_rows(documents: list[dict[str, Any]], company_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in documents:
        rows.append(
            {
                "id": document.get("article_id"),
                "company_id": company_id,
                "title": document.get("title"),
                "source_url": document.get("url"),
                "fetch_url": document.get("fetch_url"),
                "content_type": document.get("content_type"),
                "source_content_type": document.get("source_content_type"),
                "source_file": document.get("source_file"),
                "source_row": document.get("source_row"),
                "content_hash": document.get("content_hash"),
                "text_chars": document.get("text_chars"),
                "chunk_count": document.get("chunk_count"),
                "skipped_price_chunks": document.get("skipped_price_chunks"),
                "metadata": {
                    "staging_source": "documents.corpus.jsonl",
                    "status_code": document.get("status_code"),
                },
            }
        )
    return rows


def _chunk_rows(chunks: list[dict[str, Any]], company_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        rows.append(
            {
                "id": chunk.get("chunk_id"),
                "document_id": chunk.get("article_id"),
                "company_id": company_id,
                "chunk_index": chunk.get("chunk_index"),
                "source_url": chunk.get("url"),
                "title": chunk.get("title"),
                "content": text,
                "content_hash": _hash_text(text),
                "text_chars": chunk.get("text_chars"),
                "embedding": None,
                "embedding_model": None,
                "metadata": {
                    "staging_source": "chunks.corpus.jsonl",
                    **(chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}),
                },
            }
        )
    return rows


def _report(documents: list[dict[str, Any]], chunks: list[dict[str, Any]], company_id: str) -> str:
    total_chars = sum(int(chunk.get("text_chars") or 0) for chunk in chunks)
    return "\n".join(
        [
            "# RAG pgvector Payload Report",
            "",
            f"- company_id: {company_id}",
            f"- documents: {len(documents)}",
            f"- chunks: {len(chunks)}",
            f"- chunk chars: {total_chars}",
            "- embeddings: not generated in this step",
            "- prices/services remain outside RAG and must come from structured KB",
            "",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Подготовить JSONL payload для future pgvector ingestion.")
    parser.add_argument("--company", default="rosh_import_demo")
    parser.add_argument("--crawl-dir", type=Path, default=DEFAULT_CRAWL_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    documents = _read_jsonl(args.crawl_dir / "documents.corpus.jsonl")
    chunks = _read_jsonl(args.crawl_dir / "chunks.corpus.jsonl")
    document_rows = _document_rows(documents, args.company)
    chunk_rows = _chunk_rows(chunks, args.company)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(args.output_dir / "rag_documents.jsonl", document_rows)
    _write_jsonl(args.output_dir / "rag_chunks.jsonl", chunk_rows)
    (args.output_dir / "payload_report.md").write_text(
        _report(document_rows, chunk_rows, args.company),
        encoding="utf-8",
    )

    print(f"written: {args.output_dir / 'rag_documents.jsonl'}")
    print(f"written: {args.output_dir / 'rag_chunks.jsonl'}")
    print(f"written: {args.output_dir / 'payload_report.md'}")
    print(f"documents: {len(document_rows)}; chunks: {len(chunk_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
