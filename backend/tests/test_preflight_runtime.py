"""preflight, часть 1б: тихие сбои — упавшие фоновые задачи, откат LLM, опрос Telegram, права бота,
CORS, отпечатки кода и данных, память контейнера, расчёт часов работы."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.runtime_stats import STATS, RuntimeStats

HEADERS = {"X-Operator-Token": "demo-operator-token"}


@pytest.fixture(autouse=True)
def _reset_stats():
    STATS.reset()
    yield
    STATS.reset()


def _report(client, path: str = "/api/debug/preflight?network=0") -> dict:
    return client.get(path, headers=HEADERS).json()


class _Bridge:
    """подставной бот с учётом опроса и (по желанию) правами в группе."""

    def __init__(self, *, capabilities=None, capabilities_error: Exception | None = None, poll=None) -> None:
        self.enabled = True
        self._capabilities = capabilities
        self._capabilities_error = capabilities_error
        if poll is not None:
            self.last_poll_ok_at = poll.get("last_ok")
            self.consecutive_poll_failures = poll.get("failures", 0)
            self.last_poll_error = poll.get("error")
        if capabilities is not None or capabilities_error is not None:
            self.group_capabilities = self._group_capabilities

    async def health_check(self) -> dict:
        return {
            "enabled": True,
            "bot_token": {"status": "ok", "detail": "bot username: @test_bot"},
            "operators_group": {"status": "ok", "detail": "доступ есть, status=administrator"},
        }

    async def _group_capabilities(self) -> dict:
        if self._capabilities_error is not None:
            raise self._capabilities_error
        return self._capabilities


def _install(client, monkeypatch: pytest.MonkeyPatch, bridge: _Bridge) -> None:
    settings = client.app.state.settings
    monkeypatch.setattr(client.app.state, "telegram_bridge_service", bridge)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-bot-token")
    monkeypatch.setattr(settings, "telegram_operators_group_id", "-1001")
    monkeypatch.setattr(settings, "telegram_clients_topic_id", "42")


# ---------------------------------------------------------------- фоновые задачи


def test_background_tasks_are_registered_and_reported(test_client) -> None:
    tasks = {task["name"]: task for task in _report(test_client)["checks"]["background_tasks"]["tasks"]}

    assert tasks["leads_archive"]["status"] == "ok"
    assert tasks["analytics_prune"]["status"] == "ok"
    assert tasks["delivery_retry"]["status"] == "skip"  # в тестовом окружении выключен настройкой
    assert tasks["telegram_polling"]["status"] == "skip"  # нет токена: цикл завершается сразу, это не сбой


def test_crashed_background_task_is_error(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(boom())
        with pytest.raises(RuntimeError):
            loop.run_until_complete(task)
    finally:
        loop.close()
    monkeypatch.setitem(test_client.app.state.background_tasks, "leads_archive", task)

    payload = _report(test_client)

    group = payload["checks"]["background_tasks"]
    assert group["status"] == "error"
    assert "RuntimeError" in next(t for t in group["tasks"] if t["name"] == "leads_archive")["detail"]
    assert payload["status"] == "error"


def test_missing_enabled_task_is_error(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(test_client.app.state.background_tasks, "analytics_prune")

    tasks = {t["name"]: t for t in _report(test_client)["checks"]["background_tasks"]["tasks"]}

    assert tasks["analytics_prune"]["status"] == "error"


# ---------------------------------------------------------------- LLM


def test_llm_runtime_is_skipped_for_mock(test_client) -> None:
    assert _report(test_client)["checks"]["llm_runtime"]["status"] == "skip"


def _real_llm(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.app.state, "llm_client", object())


def test_llm_runtime_states(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    _real_llm(test_client, monkeypatch)

    assert _report(test_client)["checks"]["llm_runtime"]["status"] == "ok"

    STATS.record_ok("llm.complete")
    STATS.record_ok("llm.classify")
    assert _report(test_client)["checks"]["llm_runtime"]["calls_ok"] == 2

    STATS.record_error("llm.complete", TimeoutError("x"))
    degraded = _report(test_client)["checks"]["llm_runtime"]
    assert degraded["status"] == "degraded"
    assert degraded["last_error_type"] == "TimeoutError"

    STATS.reset()
    STATS.record_error("llm.complete", "HTTPStatusError")
    STATS.record_error("llm.classify", "HTTPStatusError")
    failing = _report(test_client)
    assert failing["checks"]["llm_runtime"]["status"] == "error"
    assert "шаблонами" in failing["checks"]["llm_runtime"]["detail"]
    assert failing["status"] == "error"


def test_runtime_stats_window_and_reset() -> None:
    stats = RuntimeStats(window_seconds=3600, max_events=3)

    for _ in range(5):
        stats.record_ok("x")
    stats.record_error("x", ValueError("bad"))

    summary = stats.summary("x")["x"]
    assert summary["ok"] + summary["errors"] == 3  # хранятся только последние max_events
    assert summary["errors"] == 1
    assert summary["last_error_type"] == "ValueError"
    stats.reset()
    assert stats.summary() == {}


def test_safe_complete_records_success_and_fallback() -> None:
    from app.routes.chat_utils import safe_complete

    class Failing:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("llm down")

    class Working:
        async def complete(self, *args, **kwargs):
            return "готовый ответ"

    def request_with(client) -> SimpleNamespace:
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(llm_client=client, system_prompt="")))

    ok_answer = asyncio.run(safe_complete(request_with(Working()), {}, "привет", []))
    fallback_answer = asyncio.run(safe_complete(request_with(Failing()), {}, "привет", []))

    assert ok_answer == "готовый ответ"
    assert fallback_answer  # ушли в шаблонный ответ, а не упали
    summary = STATS.summary("llm.")["llm.complete"]
    assert summary["ok"] == 1
    assert summary["errors"] == 1
    assert summary["last_error_type"] == "RuntimeError"


# ---------------------------------------------------------------- опрос Telegram и права бота


@pytest.mark.parametrize(
    ("poll", "expected"),
    [
        ({"last_ok": None}, "ok"),  # только что запущен: ещё рано судить
        ({"last_ok": "fresh"}, "ok"),
        ({"last_ok": "stale_degraded"}, "degraded"),
        ({"last_ok": "stale_error"}, "error"),
        ({"last_ok": "fresh", "failures": 3, "error": "Conflict"}, "degraded"),
    ],
)
def test_poll_status(test_client, monkeypatch: pytest.MonkeyPatch, poll, expected) -> None:
    now = time.time()
    monkeypatch.setattr("app.preflight.PROCESS_STARTED_AT", now)  # «только что запущен» не зависит от длины прогона тестов
    poll = dict(poll)
    poll["last_ok"] = {
        None: None,
        "fresh": now - 5,
        "stale_degraded": now - 300,
        "stale_error": now - 1200,
    }[poll["last_ok"]]
    _install(test_client, monkeypatch, _Bridge(poll=poll))

    telegram = _report(test_client)["checks"]["telegram"]

    assert telegram["polling"]["status"] == expected
    assert telegram["status"] == expected


def test_poll_never_succeeded_after_grace_is_degraded(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight.PROCESS_STARTED_AT", time.time() - 3600)
    _install(test_client, monkeypatch, _Bridge(poll={"last_ok": None, "error": "network_error:ConnectError"}))

    telegram = _report(test_client)["checks"]["telegram"]

    assert telegram["status"] == "degraded"
    assert "ConnectError" in telegram["detail"]


@pytest.mark.parametrize(
    ("capabilities", "expected", "needle"),
    [
        ({"is_forum": True, "member_status": "administrator", "can_manage_topics": True}, "ok", "режим тем включён"),
        ({"is_forum": False, "member_status": "administrator", "can_manage_topics": True}, "error", "не в режиме тем"),
        ({"is_forum": True, "member_status": "member", "can_manage_topics": None}, "error", "не администратор"),
        ({"is_forum": True, "member_status": "administrator", "can_manage_topics": False}, "error", "права управлять темами"),
        ({"is_forum": True, "member_status": "creator", "can_manage_topics": None}, "ok", "режим тем включён"),
    ],
)
def test_group_capabilities_verdict(test_client, monkeypatch: pytest.MonkeyPatch, capabilities, expected, needle) -> None:
    _install(test_client, monkeypatch, _Bridge(capabilities=capabilities))

    telegram = _report(test_client, "/api/debug/preflight")["checks"]["telegram"]

    assert telegram["status"] == expected
    assert needle in telegram["detail"]
    assert telegram["group_capabilities"]["is_forum"] is capabilities["is_forum"]


def test_group_capabilities_failure_is_degraded(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(test_client, monkeypatch, _Bridge(capabilities_error=RuntimeError("x")))

    telegram = _report(test_client, "/api/debug/preflight")["checks"]["telegram"]

    assert telegram["status"] == "degraded"
    assert "права бота не проверены" in telegram["detail"]


def _bridge_service():
    from app.sessions import SessionStore
    from app.telegram_bridge import TelegramBridgeService

    return TelegramBridgeService(bot_token="token", group_chat_id="-1001", session_store=SessionStore(), ws_manager=None)


def test_bridge_group_capabilities_reads_forum_and_topic_rights(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bridge_service()
    calls: list[str] = []

    async def fake_call(method: str, **params):
        calls.append(method)
        return {
            "getChat": {"ok": True, "result": {"is_forum": True}},
            "getMe": {"ok": True, "result": {"id": 7}},
            "getChatMember": {"ok": True, "result": {"status": "administrator", "can_manage_topics": True}},
        }[method]

    monkeypatch.setattr(service, "_call", fake_call)

    result = asyncio.run(service.group_capabilities())

    assert result == {"is_forum": True, "member_status": "administrator", "can_manage_topics": True}
    assert set(calls) == {"getChat", "getMe", "getChatMember"}  # только read-only методы


def test_bridge_group_capabilities_reports_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bridge_service()

    async def fake_call(method: str, **params):
        return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(service, "_call", fake_call)

    result = asyncio.run(service.group_capabilities())

    assert result["is_forum"] is None
    assert result["error"] == "chat not found"


def test_polling_loop_tracks_success_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bridge_service()
    responses = [
        {"ok": False, "description": "Conflict: terminated by other getUpdates request"},
        {"ok": False, "description": "Conflict: terminated by other getUpdates request"},
        {"ok": True, "result": []},
    ]

    async def fake_call(method: str, **params):
        if not responses:
            raise asyncio.CancelledError
        return responses.pop(0)

    monkeypatch.setattr(service, "_call", fake_call)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_polling_loop())

    assert service.last_poll_ok_at is not None
    assert service.consecutive_poll_failures == 0  # успешный опрос сбрасывает счётчик
    assert service.last_poll_error == "Conflict: terminated by other getUpdates request"


def test_polling_loop_counts_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _bridge_service()
    responses = [{"ok": False, "description": "network_error:ConnectError"}] * 3

    async def fake_call(method: str, **params):
        if not responses:
            raise asyncio.CancelledError
        return responses.pop(0)

    monkeypatch.setattr(service, "_call", fake_call)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_polling_loop())

    assert service.last_poll_ok_at is None
    assert service.consecutive_poll_failures == 3


# ---------------------------------------------------------------- CORS, отпечатки, ресурсы, часы


def test_cors_flags_client_domains_missing_from_allowed_origins(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = test_client.app.state.settings
    domains = [d["domain"] for d in _report(test_client)["checks"]["domains"]["domains"]]
    assert domains, "в тестовых клиентах должен быть хотя бы один не-localhost домен"

    monkeypatch.setattr(settings, "allowed_origins", "http://localhost:8000")
    assert _report(test_client)["checks"]["cors"]["status"] == "degraded"

    monkeypatch.setattr(settings, "allowed_origins", ",".join(f"https://{domain}" for domain in domains))
    assert _report(test_client)["checks"]["cors"]["status"] == "ok"

    monkeypatch.setattr(settings, "allowed_origins", "*")
    assert _report(test_client)["checks"]["cors"]["status"] == "ok"


def test_fingerprint_changes_with_content_and_ignores_pycache(tmp_path) -> None:
    from app.preflight import _fingerprint

    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("print(2)\n", encoding="utf-8")
    first = _fingerprint(tmp_path, "*.py")

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.py").write_text("x", encoding="utf-8")
    assert _fingerprint(tmp_path, "*.py") == first

    (tmp_path / "b.py").write_text("print(3)\n", encoding="utf-8")
    assert _fingerprint(tmp_path, "*.py") != first


def test_report_carries_code_and_data_fingerprints(test_client) -> None:
    first = _report(test_client)
    second = _report(test_client)

    code = first["checks"]["app"]["code_fingerprint"]
    assert len(code) == 12
    assert code == second["checks"]["app"]["code_fingerprint"]
    rosh = next(c for c in first["checks"]["clients"]["clients"] if c["company_id"] == "rosh_demo")
    assert len(rosh["data_fingerprint"]) == 12
    assert rosh["data_fingerprint"] == next(
        c for c in second["checks"]["clients"]["clients"] if c["company_id"] == "rosh_demo"
    )["data_fingerprint"]


def test_client_report_includes_working_hours_state(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    rosh = next(c for c in _report(test_client)["checks"]["clients"]["clients"] if c["company_id"] == "rosh_demo")
    assert isinstance(rosh["open_now"], bool)

    def broken(*args, **kwargs):
        raise KeyError("Europe/Nowhere")

    monkeypatch.setattr("app.preflight.is_currently_open", broken)
    rosh = next(c for c in _report(test_client)["checks"]["clients"]["clients"] if c["company_id"] == "rosh_demo")
    assert rosh["status"] == "degraded"
    assert "расчёт часов работы падает" in rosh["detail"]


@pytest.mark.parametrize(
    ("used_mb", "expected"),
    [(500, "ok"), (900, "degraded"), (980, "error")],
)
def test_container_memory_thresholds(test_client, monkeypatch: pytest.MonkeyPatch, used_mb, expected) -> None:
    monkeypatch.setattr("app.preflight._container_memory", lambda: (used_mb * 1024 * 1024, 1000 * 1024 * 1024))

    resources = _report(test_client)["checks"]["resources"]

    assert resources["status"] == expected
    assert resources["container_limit_mb"] == 1000.0


def test_unwritable_overrides_dir_is_degraded(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.preflight._container_memory", lambda: None)
    monkeypatch.setattr("app.preflight._writable", lambda path: "overrides" not in str(path))

    resources = _report(test_client)["checks"]["resources"]

    assert resources["status"] == "degraded"
    assert resources["overrides_writable"] is False
