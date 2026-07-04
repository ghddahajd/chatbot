"""Проверяет retrieval по staging article chunks без embeddings/pgvector."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_CRAWL_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "crawl"
DEFAULT_INPUT = (
    DEFAULT_CRAWL_DIR / "chunks.corpus.jsonl"
    if (DEFAULT_CRAWL_DIR / "chunks.corpus.jsonl").exists()
    else DEFAULT_CRAWL_DIR / "chunks.jsonl"
)
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "retrieval"
DEFAULT_QUERIES = [
    "как проходит кольпоскопия",
    "что нельзя после лазерной шлифовки",
    "как работает внутриматочная спираль",
    "ботулинотерапия при мигрени",
    "подбор контрацептивов обследования",
    "лечение недержания мочи гиалуроновой кислотой",
    "уход за кожей после лета",
    "филлеры в гинекологии",
]
STOP_WORDS = {
    "а",
    "без",
    "в",
    "во",
    "для",
    "до",
    "если",
    "и",
    "или",
    "как",
    "на",
    "не",
    "о",
    "об",
    "от",
    "по",
    "после",
    "при",
    "про",
    "с",
    "со",
    "что",
    "это",
}


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9\s-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in _normalize(value).split()
        if len(token) >= 3 and token not in STOP_WORDS
    ]


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: expected object")
        chunks.append(payload)
    return chunks


def _document_frequencies(chunks: list[dict[str, Any]]) -> Counter[str]:
    frequencies: Counter[str] = Counter()
    for chunk in chunks:
        text = f"{chunk.get('title') or ''} {chunk.get('text') or ''}"
        frequencies.update(set(_tokens(text)))
    return frequencies


def _snippet(text: str, query_tokens: list[str], size: int = 360) -> str:
    normalized_text = _normalize(text)
    first_match = len(text) // 3
    for token in query_tokens:
        index = normalized_text.find(token)
        if index >= 0:
            first_match = index
            break
    start = max(0, first_match - size // 3)
    end = min(len(text), start + size)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


def _score_chunk(
    chunk: dict[str, Any],
    query: str,
    query_tokens: list[str],
    document_frequencies: Counter[str],
    total_chunks: int,
) -> float:
    text = str(chunk.get("text") or "")
    title = str(chunk.get("title") or "")
    normalized_text = _normalize(text)
    normalized_title = _normalize(title)
    chunk_tokens = Counter(_tokens(text))

    score = 0.0
    for token in query_tokens:
        tf = chunk_tokens[token]
        if tf <= 0:
            continue
        idf = math.log((total_chunks + 1) / (document_frequencies[token] + 1)) + 1
        score += (1 + math.log(tf)) * idf
        if token in normalized_title:
            score += 2.5

    normalized_query = _normalize(query)
    if normalized_query and normalized_query in normalized_text:
        score += 6.0
    if query_tokens and all(token in normalized_text for token in query_tokens):
        score += 3.0
    return round(score, 4)


def _search(chunks: list[dict[str, Any]], queries: list[str], top_k: int) -> list[dict[str, Any]]:
    document_frequencies = _document_frequencies(chunks)
    results: list[dict[str, Any]] = []
    for query in queries:
        query_tokens = _tokens(query)
        scored: list[dict[str, Any]] = []
        for chunk in chunks:
            score = _score_chunk(chunk, query, query_tokens, document_frequencies, len(chunks))
            if score <= 0:
                continue
            scored.append(
                {
                    "score": score,
                    "chunk_id": chunk.get("chunk_id"),
                    "title": chunk.get("title"),
                    "url": chunk.get("url"),
                    "chunk_index": chunk.get("chunk_index"),
                    "snippet": _snippet(str(chunk.get("text") or ""), query_tokens),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        results.append({"query": query, "tokens": query_tokens, "matches": scored[:top_k]})
    return results


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _report(results: list[dict[str, Any]]) -> str:
    lines = [
        "# ROSH Article Retrieval Smoke",
        "",
        "Это lexical smoke без embeddings/pgvector. Цель — проверить, что chunks вообще находят релевантные источники.",
        "",
    ]
    for result in results:
        lines.extend([f"## {result['query']}", ""])
        matches = result.get("matches") or []
        if not matches:
            lines.extend(["- No matches", ""])
            continue
        for match in matches:
            lines.extend(
                [
                    f"- score={match['score']} — {match['title']} — chunk {match['chunk_index']}",
                    f"  {match['url']}",
                    f"  > {match['snippet']}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke retrieval по article chunks без embeddings.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--query", action="append", dest="queries", help="Можно передать несколько раз.")
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    chunks = _load_chunks(args.input)
    queries = args.queries or DEFAULT_QUERIES
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = _search(chunks, queries, args.top_k)
    _write_json(args.output_dir / "retrieval_results.json", results)
    (args.output_dir / "retrieval_report.md").write_text(_report(results), encoding="utf-8")

    print(f"chunks: {len(chunks)}")
    print(f"queries: {len(queries)}")
    print(f"written: {args.output_dir / 'retrieval_results.json'}")
    print(f"written: {args.output_dir / 'retrieval_report.md'}")
    for result in results:
        first = (result.get("matches") or [{}])[0]
        print(f"- {result['query']} -> {first.get('title', 'NO MATCH')} ({first.get('score', 0)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
