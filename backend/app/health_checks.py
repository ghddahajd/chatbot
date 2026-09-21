"""общий сбор проверок готовности: их использует и /health, и /api/debug/preflight.

Одна реализация на обоих — чтобы «здоровье» в мониторинге и в ручной проверке не разошлось.
Только чтение уже загруженного состояния: ни сети, ни повторного чтения тяжёлых файлов.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from .llm.mock import MockLLMClient
from .services.rag_search import rag_corpus_status


def collect_health_checks(app: FastAPI) -> dict[str, dict[str, Any]]:
    """собирает проверки компонентов из app.state (тот же набор, что раньше был внутри /health)."""

    settings = app.state.settings

    checks: dict[str, dict[str, object]] = {}

    resolver = getattr(app.state, "knowledge_base_resolver", None)
    client_ids: list[str] = []
    if resolver is not None and resolver.clients_data_dir.exists():
        client_ids = sorted(item.name for item in resolver.clients_data_dir.iterdir() if item.is_dir())
    checks["knowledge_base"] = {
        "status": "ok" if client_ids else "error",
        "clients_loaded": len(client_ids),
        "detail": ", ".join(client_ids) if client_ids else "no client directories configured",
    }

    corpus_status = getattr(app.state, "rag_corpus_status", None) or rag_corpus_status()
    chunk_count = int(corpus_status.get("chunk_count") or 0)
    if corpus_status.get("ok") and chunk_count > 0:
        rag_status, rag_detail = "ok", f"{chunk_count} chunks loaded"
    elif corpus_status.get("ok"):
        rag_status, rag_detail = "degraded", "corpus loaded but empty"
    else:
        rag_status, rag_detail = "unavailable", str(corpus_status.get("error") or "not loaded")
    checks["rag_index"] = {"status": rag_status, "chunks_loaded": chunk_count, "detail": rag_detail}

    # Живой репро (аудит §2026-08-22): раньше сравнивали settings.llm_provider == "mock" —
    # строку конфига, не то, что реально построил build_llm_client(). Незнакомый provider
    # (опечатка, неподдержанное значение) или падение каждого реального вызова в fallback
    # молча уходили в MockLLMClient, а health показывал "ok" — единственный признак был бы
    # шаблонные ответы в живых диалогах. Проверяем реальный класс объекта, не конфиг.
    llm_client = getattr(app.state, "llm_client", None)
    is_mock_llm = isinstance(llm_client, MockLLMClient)
    checks["llm_provider"] = {
        "status": "degraded" if is_mock_llm else "ok",
        "provider": settings.llm_provider,
        "detail": "mock mode — no real LLM API configured" if is_mock_llm else settings.llm_model,
    }

    delivery_service = getattr(app.state, "delivery_service", None)
    checks["delivery"] = (
        delivery_service.outbox_health()
        if delivery_service is not None
        else {"status": "ok", "pending_events": 0, "dead_events": 0}
    )

    return checks


def overall_status(checks: dict[str, dict[str, Any]]) -> tuple[str, int]:
    """общий статус и HTTP-код: error -> 503, любая деградация -> 207, иначе 200."""

    statuses = {str(check["status"]) for check in checks.values()}
    if "error" in statuses:
        return "error", 503
    if statuses - {"ok"}:
        return "degraded", 207
    return "ok", 200
