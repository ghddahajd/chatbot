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


def rag_corpus_status(path: Path | None = None) -> dict[str, Any]:
    """Статус RAG-корпуса для громкой проверки при старте/health-чека.

    Отсутствие корпуса раньше деградировало молча — retrieve_article_context ловил
    FileNotFoundError и просто отдавал пустой список, смоук-тест это не ловит (цены и
    услуги отвечают нормально, статьи тихо пропадают). Явный статус — чтобы это было видно
    сразу при деплое, а не через жалобу клиента."""

    chunks_path = path or default_rag_chunks_path()
    if not chunks_path.exists():
        return {"path": str(chunks_path), "ok": False, "chunk_count": 0, "error": "file_not_found"}
    try:
        # Тем же кэшем, что и search_rag_chunks — вызов при старте (см. lifespan в main.py)
        # заодно прогревает кэш до первого реального запроса пользователя.
        chunks, _frequencies = _load_corpus_cached(chunks_path)
    except Exception as error:
        return {"path": str(chunks_path), "ok": False, "chunk_count": 0, "error": type(error).__name__}
    if not chunks:
        return {"path": str(chunks_path), "ok": False, "chunk_count": 0, "error": "empty_corpus"}
    return {"path": str(chunks_path), "ok": True, "chunk_count": len(chunks), "error": None}


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


# Корпус (перечитать файл + пересчитать IDF по всем чанкам) стоил ~200мс и раньше делался
# ЗАНОВО на каждый вызов search_rag_chunks — а вызовов может быть до 6 на одно сообщение
# (разные ветки analyze_message независимо дёргают RAG). Корпус реально меняется только на
# деплое/перегенерации, не на каждый чих — кэшируем по пути + mtime файла (не просто по
# пути, чтобы живая перегенерация корпуса без рестарта процесса тоже подхватывалась).
_CORPUS_CACHE: dict[str, tuple[float, list[dict[str, Any]], Counter[str]]] = {}


def _load_corpus_cached(chunks_path: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    key = str(chunks_path)
    mtime = chunks_path.stat().st_mtime  # тот же FileNotFoundError, что раньше бросал load_chunks
    cached = _CORPUS_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]

    chunks = load_chunks(chunks_path)
    frequencies = _document_frequencies(chunks)
    _CORPUS_CACHE[key] = (mtime, chunks, frequencies)
    return chunks, frequencies


def clear_corpus_cache() -> None:
    """Для тестов и живой перегенерации корпуса без ожидания смены mtime."""

    _CORPUS_CACHE.clear()


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

    # Один редкий, но нерелевантный токен не должен в одиночку перетягивать порог уверенности —
    # нашли живьём: "у меня прыщи не проходят полгода" совпало со статьёй про второй подбородок
    # только по слову "полгода" (там "результат виден через полгода", про совсем другое); у
    # редкого слова высокий IDF, и одного совпадения хватило пройти MIN_ARTICLE_SCORE, хотя
    # "прыщи"/"проходят" — то есть весь смысл вопроса — в статье не встречаются вообще. Тот же
    # класс бага, что раньше чинили для местоимений в STOP_WORDS, но общим правилом, не списком
    # конкретных слов: для запроса из 2+ РАЗНЫХ слов требуем минимум 2 совпадения. Исключение —
    # единственное совпадение попало в ЗАГОЛОВОК статьи (сильный сигнал предметности, например
    # "как проходит кольпоскопия" про статью "Кольпоскопия" — "проходит" в тексте может не быть
    # вообще, но название статьи прямо отвечает на вопрос). Запросы с одним содержательным словом
    # (остальное — стоп-слова) не трогаем — требовать от них 2 совпадения нечем.
    distinct_query_tokens = set(query_tokens)
    if len(distinct_query_tokens) >= 2:
        matched_tokens = {token for token in distinct_query_tokens if chunk_tokens[token] > 0}
        if len(matched_tokens) < 2 and not any(token in normalized_title for token in matched_tokens):
            return 0.0

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
    chunks, document_frequencies = _load_corpus_cached(chunks_path)
    query_tokens = tokenize(query)

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
