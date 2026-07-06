import json
from pathlib import Path


def _write_rag_chunks(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "chunk_id": "kolpo-1",
                "document_id": "doc-kolpo",
                "title": "Как проходит кольпоскопия",
                "url": "https://example.test/kolposkopiya",
                "chunk_index": 0,
                "source_type": "article",
                "text": "Как проходит кольпоскопия: кольпоскопия помогает врачу осмотреть шейку матки и выявить изменения тканей.",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_debug_trace_requires_operator_token(test_client) -> None:
    response = test_client.post(
        "/api/debug/trace",
        json={"company_id": "rosh_demo", "message": "сколько стоит чистка лица"},
    )

    assert response.status_code == 403


def test_debug_trace_returns_decision_steps(test_client) -> None:
    response = test_client.post(
        "/api/debug/trace?token=demo-operator-token",
        json={"company_id": "rosh_demo", "message": "сколько стоит чистка лица"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["company_id"] == "rosh_demo"
    assert payload["final_answer"]
    step_names = [step["step"] for step in payload["steps"]]
    assert "classification" in step_names
    assert "restricted_check" in step_names
    assert "kb_lookup" in step_names
    assert "price_lookup" in step_names
    assert "rag_retrieval" in step_names
    assert "policy_decision" in step_names
    assert "llm_generation" in step_names
    assert "validation" in step_names


def test_debug_trace_includes_rag_retrieval_matches(test_client, tmp_path: Path, monkeypatch) -> None:
    chunks_file = tmp_path / "chunks.jsonl"
    _write_rag_chunks(chunks_file)
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(chunks_file))

    response = test_client.post(
        "/api/debug/trace?token=demo-operator-token",
        json={"company_id": "rosh_demo", "message": "как проходит кольпоскопия"},
    )

    assert response.status_code == 200
    rag_step = next(step for step in response.json()["steps"] if step["step"] == "rag_retrieval")
    assert rag_step["result"]["triggered"] is True
    assert rag_step["result"]["matches"]


def test_debug_trace_unknown_company_404(test_client) -> None:
    response = test_client.post(
        "/api/debug/trace?token=demo-operator-token",
        json={"company_id": "unknown_company", "message": "покажи услуги"},
    )

    assert response.status_code == 404


def test_debug_page_requires_operator_token(test_client) -> None:
    response = test_client.get("/debug")

    assert response.status_code == 403


def test_debug_page_renders(test_client) -> None:
    response = test_client.get("/debug?token=demo-operator-token")

    assert response.status_code == 200
    assert "Debug trace" in response.text
    assert "/api/debug/trace" in response.text
