"""общий сбор проверок готовности: их использует и /health, и /api/debug/preflight.

Одна реализация на обоих — чтобы «здоровье» в мониторинге и в ручной проверке не разошлось.
Только чтение уже загруженного состояния: ни сети, ни повторного чтения тяжёлых файлов.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from .llm.mock import MockLLMClient


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

    checks["rag_index"] = rag_index_check(client_rag_statuses(resolver))

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


def client_rag_statuses(resolver: Any) -> dict[str, dict[str, Any]]:
    """корпус статей по каждому клиенту: у каждого свой, из rag.corpus в его config.yaml."""

    if resolver is None or not resolver.clients_data_dir.exists():
        return {}
    statuses: dict[str, dict[str, Any]] = {}
    for company_id in sorted(item.name for item in resolver.clients_data_dir.iterdir() if item.is_dir()):
        try:
            statuses[company_id] = resolver.get(company_id, fallback=False).rag_status()
        except Exception as error:  # noqa: BLE001 — один сломанный клиент не должен ронять проверку остальных
            error_name = f"kb_load_failed: {type(error).__name__}"
            statuses[company_id] = {"ok": False, "chunk_count": 0, "error": error_name, "path": None}
    return statuses


def rag_index_check(statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """сводка для /health: корпус пропал или битый — unavailable; пустой или не указан — degraded."""

    # у rosh_import_demo и rosh_test один и тот же корпус — считаем его один раз
    chunks_by_path = {
        str(item["path"]): int(item.get("chunk_count") or 0)
        for item in statuses.values()
        if item.get("ok") and item.get("path")
    }
    problems: list[str] = []
    severity = 0
    for company_id, item in statuses.items():
        error = item.get("error")
        if not error:
            continue
        problems.append(f"{company_id}: {'не указан rag.corpus' if error == 'not_declared' else error}")
        severity = max(severity, 1 if error in {"not_declared", "empty_corpus"} else 2)
    if not statuses:
        severity, problems = 2, ["нет клиентов"]
    per_client = [
        f"{company_id}: {'статей нет (none)' if item.get('disabled') else str(item['chunk_count']) + ' chunks'}"
        for company_id, item in statuses.items()
        if item.get("ok")
    ]
    return {
        "status": ("ok", "degraded", "unavailable")[severity],
        "chunks_loaded": sum(chunks_by_path.values()),
        "detail": "; ".join(problems + per_client),
    }
