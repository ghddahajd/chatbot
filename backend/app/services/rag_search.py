"""Lexical search over staged RAG chunks before pgvector is wired in."""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHUNKS_PATH = (
    REPO_ROOT
    / "client-input"
    / "normalized"
    / "rosh_articles"
    / "crawl"
    / "chunks.corpus.jsonl"
)

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
    # личные/притяжательные местоимения — почти в каждом сообщении, но в TF-IDF без
    # фильтрации дают ложное совпадение с любой статьёй, где они просто есть в тексте
    # (например риторическое "у меня нормальный вес?" в статье про второй подбородок
    # совпало с "у меня голова грязная" и прошло порог MIN_ARTICLE_SCORE).
    "меня",
    "мне",
    "мной",
    "мой",
    "моя",
    "моё",
    "мои",
    "себя",
    "свой",
    "своя",
    "своё",
    "свои",
    "тебя",
    "тебе",
    "нам",
    "нас",
    "вас",
    "вам",
}
MIN_ARTICLE_SCORE = 6.0


def default_rag_chunks_path() -> Path:
    """Return configured chunks path, falling back to local staging corpus."""

    configured = os.getenv("RAG_CHUNKS_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_CHUNKS_PATH


def normalize_text(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9\s-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokenize(value: str) -> list[str]:
    return [
        token
        for token in normalize_text(value).split()
        if len(token) >= 3 and token not in STOP_WORDS
    ]


def load_chunks(path: Path | None = None) -> list[dict[str, Any]]:
    chunks_path = path or default_rag_chunks_path()
    chunks: list[dict[str, Any]] = []
    for line_number, line in enumerate(chunks_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{chunks_path}:{line_number}: expected object")
        chunks.append(payload)
    return chunks


def _document_frequencies(chunks: list[dict[str, Any]]) -> Counter[str]:
    frequencies: Counter[str] = Counter()
    for chunk in chunks:
        text = f"{chunk.get('title') or ''} {chunk.get('text') or ''}"
        frequencies.update(set(tokenize(text)))
    return frequencies


def _word_window(text: str, center: int, size: int) -> str:
    start = max(0, center - size // 3)
    end = min(len(text), start + size)
    if start > 0:
        next_space = text.find(" ", start)
        if 0 <= next_space < end:
            start = next_space + 1
    if end < len(text):
        prev_space = text.rfind(" ", start, end)
        if prev_space > start:
            end = prev_space
    return text[start:end].strip(" ,;:—-")


def _snippet(text: str, query_tokens: list[str], size: int = 420) -> str:
    clean_text = re.sub(r"\s+", " ", text).strip()
    if not clean_text:
        return ""

    normalized_text = clean_text.lower().replace("ё", "е")
    first_match = len(clean_text) // 3
    for token in query_tokens:
        index = normalized_text.find(token)
        if index >= 0:
            first_match = index
            break

    sentence_start = max(
        clean_text.rfind(".", 0, first_match),
        clean_text.rfind("!", 0, first_match),
        clean_text.rfind("?", 0, first_match),
    )
    sentence_start = 0 if sentence_start < 0 else sentence_start + 1

    # набираем СКОЛЬКО ПОЛУЧИТСЯ полных предложений подряд (не одно), пока не подойдём
    # к бюджету size — иначе модели не хватает контекста для содержательного ответа,
    # а граница всё равно остаётся на конце предложения, не обрывается на полуслове
    end = sentence_start
    while end - sentence_start < size:
        sentence_ends = [
            index
            for index in (
                clean_text.find(".", end),
                clean_text.find("!", end),
                clean_text.find("?", end),
            )
            if index >= 0
        ]
        if not sentence_ends:
            end = len(clean_text)
            break
        next_end = min(sentence_ends) + 1
        if next_end - sentence_start > size and end > sentence_start:
            break
        end = next_end

    snippet = clean_text[sentence_start:end].strip(" ,;:—-")
    if len(snippet) >= 40:
        return snippet

    return _word_window(clean_text, first_match, size)


def _score_chunk(
    chunk: dict[str, Any],
    query: str,
    query_tokens: list[str],
    document_frequencies: Counter[str],
    total_chunks: int,
) -> float:
    text = str(chunk.get("text") or "")
    title = str(chunk.get("title") or "")
    normalized_text = normalize_text(text)
    normalized_title = normalize_text(title)
    chunk_tokens = Counter(tokenize(text))

    score = 0.0
    for token in query_tokens:
        tf = chunk_tokens[token]
        if tf <= 0:
            continue
        idf = math.log((total_chunks + 1) / (document_frequencies[token] + 1)) + 1
        score += (1 + math.log(tf)) * idf
        if token in normalized_title:
            score += 2.5

    normalized_query = normalize_text(query)
    if normalized_query and normalized_query in normalized_text:
        score += 6.0
    if query_tokens and all(token in normalized_text for token in query_tokens):
        score += 3.0
    return round(score, 4)


def search_rag_chunks(query: str, top_k: int = 5, path: Path | None = None) -> dict[str, Any]:
    chunks_path = path or default_rag_chunks_path()
    chunks = load_chunks(chunks_path)
    query_tokens = tokenize(query)
    document_frequencies = _document_frequencies(chunks)

    matches: list[dict[str, Any]] = []
    for chunk in chunks:
        score = _score_chunk(chunk, query, query_tokens, document_frequencies, len(chunks))
        if score <= 0:
            continue
        matches.append(
            {
                "score": score,
                "chunk_id": chunk.get("chunk_id"),
                "document_id": chunk.get("document_id"),
                "title": chunk.get("title"),
                "url": chunk.get("url"),
                "chunk_index": chunk.get("chunk_index"),
                "source_type": chunk.get("source_type"),
                "snippet": _snippet(str(chunk.get("text") or ""), query_tokens),
            }
        )

    matches.sort(key=lambda item: item["score"], reverse=True)
    return {
        "query": query,
        "tokens": query_tokens,
        "chunks_file": str(chunks_path),
        "total_chunks": len(chunks),
        "matches": matches[:top_k],
    }


def retrieve_article_context(
    query: str,
    top_k: int = 3,
    min_score: float = MIN_ARTICLE_SCORE,
) -> list[dict[str, Any]]:
    """Return confident article matches for safe_context."""

    results = search_rag_chunks(query=query, top_k=top_k)
    matches = []
    for match in results.get("matches", []):
        if float(match.get("score") or 0.0) < min_score:
            continue
        matches.append(
            {
                "title": match.get("title"),
                "url": match.get("url"),
                "snippet": match.get("snippet"),
                "chunk_id": match.get("chunk_id"),
                "score": match.get("score"),
            }
        )
    return matches
