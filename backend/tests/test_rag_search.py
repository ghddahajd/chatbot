"""проверки RAG lexical retriever."""

import json
from pathlib import Path

import pytest

import app.services.rag_search as rag_search_module
from app.services.rag_search import clear_corpus_cache, rag_corpus_status, retrieve_article_context, search_rag_chunks


def _write_chunks(path: Path) -> None:
    rows = [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "title": "Кольпоскопия",
            "url": "https://example.test/kolposkopiya",
            "chunk_index": 0,
            "source_type": "article",
            "text": "Кольпоскопия помогает врачу осмотреть шейку матки и выявить изменения тканей.",
        },
        {
            "chunk_id": "chunk-2",
            "document_id": "doc-2",
            "title": "Уход за кожей",
            "url": "https://example.test/skin",
            "chunk_index": 0,
            "source_type": "article",
            "text": "После лета коже часто требуется мягкое восстановление и увлажнение.",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_retrieve_article_context_filters_confident_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    _write_chunks(chunks_file)
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(chunks_file))

    matches = retrieve_article_context("как проходит кольпоскопия", top_k=3, min_score=1.0)

    assert matches
    assert matches[0]["title"] == "Кольпоскопия"
    assert set(matches[0]) == {"title", "url", "snippet", "chunk_id", "score"}


def test_retrieve_article_context_snippet_starts_on_sentence_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    long_prefix = " ".join(["служебный текст"] * 40)
    rows = [
        {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "title": "Ксеомин",
            "url": "https://example.test/kseomin",
            "chunk_index": 0,
            "source_type": "article",
            "text": (
                f"{long_prefix}. "
                "Ксеомин отличается отсутствием комплексообразующих белков и применяется в косметологии. "
                "Подробности обсуждаются на консультации."
            ),
        }
    ]
    chunks_file.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(chunks_file))

    matches = retrieve_article_context("чем отличается ксеомин", top_k=1, min_score=1.0)

    assert matches[0]["snippet"].startswith("Ксеомин отличается")
    assert not matches[0]["snippet"].startswith("...")


def test_retrieve_article_context_returns_empty_when_score_is_low(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    _write_chunks(chunks_file)
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(chunks_file))

    assert retrieve_article_context("как проходит кольпоскопия", min_score=9999.0) == []


def test_retrieve_article_context_raises_for_missing_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(tmp_path / "missing.jsonl"))

    with pytest.raises(FileNotFoundError):
        retrieve_article_context("как проходит кольпоскопия")


def test_rag_corpus_status_ok_for_real_corpus(tmp_path: Path) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    _write_chunks(chunks_file)

    status = rag_corpus_status(chunks_file)

    assert status["ok"] is True
    assert status["chunk_count"] > 0
    assert status["error"] is None


def test_rag_corpus_status_reports_missing_file_instead_of_raising(tmp_path: Path) -> None:
    """Раньше отсутствие корпуса ловилось только внутри retrieve_article_context и тихо
    деградировало (bare except FileNotFoundError -> []). Статус должен явно сказать, что
    корпуса нет, не бросать исключение — это для лога при старте и /health, не для отказа."""

    status = rag_corpus_status(tmp_path / "missing.jsonl")

    assert status["ok"] is False
    assert status["error"] == "file_not_found"
    assert status["chunk_count"] == 0


def test_search_rag_chunks_reads_file_once_across_repeated_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Раньше load_chunks (перечитать файл) + _document_frequencies (пересчитать IDF по
    всем чанкам) выполнялись заново на КАЖДЫЙ вызов search_rag_chunks — а вызовов до 6 на
    одно сообщение. Кэш по пути+mtime должен читать файл один раз, не на каждый запрос."""

    clear_corpus_cache()
    chunks_file = tmp_path / "chunks.jsonl"
    _write_chunks(chunks_file)

    read_calls = {"count": 0}
    original_load_chunks = rag_search_module.load_chunks

    def _counting_load_chunks(path=None):
        read_calls["count"] += 1
        return original_load_chunks(path)

    monkeypatch.setattr(rag_search_module, "load_chunks", _counting_load_chunks)

    search_rag_chunks("кольпоскопия", path=chunks_file)
    search_rag_chunks("уход за кожей", path=chunks_file)
    search_rag_chunks("кольпоскопия матки", path=chunks_file)

    assert read_calls["count"] == 1


def test_search_rag_chunks_reloads_after_file_changes(tmp_path: Path) -> None:
    """Кэш не должен залипать навсегда — если корпус реально перегенерировали (mtime
    изменился), новый вызов должен подхватить новое содержимое, не старое из кэша."""

    clear_corpus_cache()
    chunks_file = tmp_path / "chunks.jsonl"
    _write_chunks(chunks_file)
    search_rag_chunks("кольпоскопия", path=chunks_file)

    import os
    import time

    time.sleep(0.01)
    chunks_file.write_text(
        json.dumps(
            {
                "chunk_id": "chunk-new",
                "document_id": "doc-new",
                "title": "Новая статья",
                "url": "https://example.test/new",
                "chunk_index": 0,
                "source_type": "article",
                "text": "Новая статья про совершенно другую тему поиска.",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(chunks_file, None)

    result = search_rag_chunks("новая статья другую тему", path=chunks_file)

    assert result["total_chunks"] == 1
    assert result["matches"][0]["chunk_id"] == "chunk-new"


def test_rag_corpus_status_reports_empty_corpus(tmp_path: Path) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    chunks_file.write_text("", encoding="utf-8")

    status = rag_corpus_status(chunks_file)

    assert status["ok"] is False
    assert status["error"] == "empty_corpus"
