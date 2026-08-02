"""проверки RAG lexical retriever."""

import json
from pathlib import Path

import pytest

from app.services.rag_search import rag_corpus_status, retrieve_article_context


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


def test_rag_corpus_status_reports_empty_corpus(tmp_path: Path) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    chunks_file.write_text("", encoding="utf-8")

    status = rag_corpus_status(chunks_file)

    assert status["ok"] is False
    assert status["error"] == "empty_corpus"
