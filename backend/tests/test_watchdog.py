"""сторож: сам гоняет проверки preflight и пишет нам о поломках и починках — не спамит разовыми
сбоями, помнит состояние после перезапуска, раз в сутки присылает сводку."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import ops_alerts
from app.ops_alerts import OpsAlerts
from app.preflight import check_llm_runtime
from app.runtime_stats import STATS
from app.utils.jsonl import append_jsonl, read_jsonl
from app.watchdog import MSK, Watchdog, leaves, ping_llm_if_idle, yesterday_activity

T0 = datetime(2026, 9, 24, 16, 12, tzinfo=timezone.utc)  # 19:12 МСК


class _Alerts:
    project = "РОШ"
    enabled = True

    def __init__(self, *, works: bool = True) -> None:
        self.works = works
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self.works:
            self.sent.append(text)
        return self.works


def _report(**statuses: str) -> dict:
    checks = {name: {"status": status, "detail": f"{name}: {status}"} for name, status in statuses.items()}
    return {"checks": checks}


def _watchdog(tmp_path, alerts=None, **kwargs) -> Watchdog:
    return Watchdog(
        alerts=alerts or _Alerts(),
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        **kwargs,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- что считаем отдельной проверкой


def test_nested_items_are_watched_separately_and_the_parent_is_not_doubled() -> None:
    report = {
        "checks": {
            "background_tasks": {
                "status": "error",
                "detail": "работают 4 из 5",
                "tasks": [{"name": "telegram_polling", "status": "error", "detail": "задача упала"}, {"name": "leads_archive", "status": "ok"}],
            },
            "clients": {"status": "degraded", "detail": "default_company нет", "clients": [{"company_id": "rosh", "status": "ok"}]},
            "watchdog": {"status": "degraded", "detail": "проблемы: …"},
        }
    }

    found = leaves(report)

    assert found["tasks:telegram_polling"]["status"] == "error"
    assert "background_tasks" not in found  # та же поломка, второе сообщение не нужно
    assert found["clients"]["status"] == "degraded"  # у клиента всё хорошо — значит, беда в самом пункте
    assert "watchdog" not in found


# ---------------------------------------------------------------- переходы и оповещения


def test_a_single_failed_run_does_not_wake_anyone(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = _watchdog(tmp_path, alerts)

    _run(watchdog.evaluate(_report(telegram="error"), now=T0))
    _run(watchdog.evaluate(_report(telegram="ok"), now=T0 + timedelta(minutes=10)))

    assert alerts.sent == []
    assert [event["event"] for event in read_jsonl(tmp_path / "events.jsonl")] == ["problem", "recovered"]


def test_problem_confirmed_on_second_run_then_recovery_with_duration(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = _watchdog(tmp_path, alerts)

    _run(watchdog.evaluate(_report(llm_runtime="error"), now=T0))
    _run(watchdog.evaluate(_report(llm_runtime="error"), now=T0 + timedelta(minutes=10)))
    _run(watchdog.evaluate(_report(llm_runtime="ok"), now=T0 + timedelta(days=3, hours=17)))

    assert alerts.sent == [
        "🔴 РОШ · нейросеть — llm_runtime: error (с 24.09 19:12 МСК)",
        "✅ РОШ · нейросеть — снова в порядке, проблема длилась 3 дн 17 ч",
    ]


def test_reminder_every_six_hours_while_still_broken(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = _watchdog(tmp_path, alerts)
    for minutes in (0, 10, 60, 5 * 60):
        _run(watchdog.evaluate(_report(storage="degraded"), now=T0 + timedelta(minutes=minutes)))
    assert len(alerts.sent) == 1

    _run(watchdog.evaluate(_report(storage="degraded"), now=T0 + timedelta(hours=6, minutes=10)))

    assert len(alerts.sent) == 2
    assert alerts.sent[-1].startswith("🟠 РОШ · диск — всё ещё не в порядке, уже 6 ч 10 мин")


def test_failed_send_is_retried_next_run(tmp_path) -> None:
    alerts = _Alerts(works=False)
    watchdog = _watchdog(tmp_path, alerts)
    for minutes in (0, 10):
        _run(watchdog.evaluate(_report(telegram="error"), now=T0 + timedelta(minutes=minutes)))
    assert alerts.sent == []

    alerts.works = True
    _run(watchdog.evaluate(_report(telegram="error"), now=T0 + timedelta(minutes=20)))

    assert len(alerts.sent) == 1


def test_state_survives_restart_no_repeat_and_recovery_still_reported(tmp_path) -> None:
    first = _Alerts()
    watchdog = _watchdog(tmp_path, first)
    for minutes in (0, 10):
        _run(watchdog.evaluate(_report(llm_runtime="error"), now=T0 + timedelta(minutes=minutes)))
    assert len(first.sent) == 1

    after_restart = _Alerts()
    watchdog = _watchdog(tmp_path, after_restart)
    _run(watchdog.evaluate(_report(llm_runtime="error"), now=T0 + timedelta(minutes=30)))
    assert after_restart.sent == []  # та же проблема после выкатки заново не приходит

    _run(watchdog.evaluate(_report(llm_runtime="ok"), now=T0 + timedelta(hours=2)))
    assert after_restart.sent == ["✅ РОШ · нейросеть — снова в порядке, проблема длилась 2 ч"]


def test_problems_are_only_confirmed_ones(tmp_path) -> None:
    watchdog = _watchdog(tmp_path)

    _run(watchdog.evaluate(_report(telegram="error", storage="ok"), now=T0))
    assert watchdog.problems() == []
    _run(watchdog.evaluate(_report(telegram="error", storage="ok"), now=T0 + timedelta(minutes=10)))

    assert watchdog.problems() == ["telegram"]


# ---------------------------------------------------------------- утренняя сводка


def test_digest_once_a_day_after_ten_moscow_time(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = _watchdog(tmp_path, alerts)
    morning = datetime(2026, 9, 25, 9, 50, tzinfo=MSK)

    assert not watchdog.digest_due(morning)
    assert watchdog.digest_due(morning + timedelta(minutes=15))
    _run(watchdog.send_digest(["Вчера диалогов не было."], now=morning + timedelta(minutes=15)))

    assert alerts.sent == ["☀️ РОШ — утренняя сводка\nВсё в порядке.\nВчера диалогов не было."]
    assert not watchdog.digest_due(morning + timedelta(hours=5))
    assert watchdog.digest_due(morning + timedelta(days=1, minutes=15))


def test_digest_lists_current_problems(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = _watchdog(tmp_path, alerts)
    for minutes in (0, 10):
        _run(watchdog.evaluate(_report(llm_runtime="error"), now=T0 + timedelta(minutes=minutes)))

    _run(watchdog.send_digest([], now=datetime(2026, 9, 25, 10, 5, tzinfo=MSK)))

    assert "Проблемы сейчас: нейросеть (с 24.09 19:12)" in alerts.sent[-1]


def test_yesterday_activity_counts_dialogs_leads_and_changes_by_moscow_day(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    yesterday = datetime(2026, 9, 24, 12, 0)  # UTC, как пишет приложение
    for session in ("a", "a", "b"):
        append_jsonl(analytics_file, {"timestamp": yesterday.isoformat(), "event_type": "message_answered", "company_id": "rosh", "session_id": session})
    append_jsonl(analytics_file, {"timestamp": "2026-09-23T20:30:00", "event_type": "message_answered", "company_id": "rosh", "session_id": "late"})  # 23:30 МСК 23-го
    leads = [
        {"timestamp": yesterday.isoformat(), "company_id": "rosh", "reason": "booking"},
        {"timestamp": yesterday.isoformat(), "company_id": "rosh", "reason": "booking_change"},
    ]

    lines = yesterday_activity(analytics_file, leads, now=datetime(2026, 9, 25, 10, 0, tzinfo=MSK))

    assert lines == ["Вчера:", "· rosh: диалогов 2, заявок 1, переносов и отмен 1"]
    assert yesterday_activity(tmp_path / "none.jsonl", [], now=T0) == ["Вчера диалогов не было."]


# ---------------------------------------------------------------- нейросеть без трафика


class _App:
    class state:  # noqa: N801 — имитация app.state
        pass


class _LLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.pings = 0

    async def ping(self) -> None:
        self.pings += 1
        if self.error:
            raise self.error


@pytest.fixture()
def clean_stats():
    STATS.reset()
    yield
    STATS.reset()


def test_idle_llm_gets_a_tiny_ping_and_a_dead_key_becomes_visible(clean_stats) -> None:
    app = _App()
    app.state.llm_client = _LLM(error=RuntimeError("401"))

    _run(ping_llm_if_idle(app))

    assert app.state.llm_client.pings == 1
    assert check_llm_runtime(app)["status"] == "error"


def test_no_ping_when_real_traffic_already_checks_the_llm(clean_stats) -> None:
    app = _App()
    app.state.llm_client = _LLM()
    STATS.record_ok("llm.classify", duration_ms=500)

    _run(ping_llm_if_idle(app))

    assert app.state.llm_client.pings == 0


# ---------------------------------------------------------------- /health и отправка


def test_health_shows_the_watchdog_verdict_without_503(test_client) -> None:
    test_client.app.state.watchdog_status = {"problems": ["llm_runtime"], "checked_at": T0.isoformat()}

    response = test_client.get("/health")

    assert response.status_code == 207
    assert response.json()["checks"]["watchdog"] == {
        "status": "degraded", "detail": "проблемы: llm_runtime", "checked_at": T0.isoformat(),
    }
    test_client.app.state.watchdog_status = {"problems": [], "checked_at": T0.isoformat()}
    assert test_client.get("/health").json()["checks"]["watchdog"]["status"] == "ok"


def test_alerts_are_silent_without_token_or_chat() -> None:
    assert _run(OpsAlerts(bot_token="", chat_id="1", project="РОШ").send("x")) is False
    assert not OpsAlerts(bot_token="t", chat_id="", project="РОШ").enabled


def test_alert_goes_to_the_chat_as_plain_text(monkeypatch) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ops_alerts.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))

    sent = _run(OpsAlerts(bot_token="123:abc", chat_id="42", project="РОШ").send("🔴 РОШ · диск — мало места"))

    assert sent is True
    assert requests[0]["url"].endswith("/bot123:abc/sendMessage")
    assert requests[0]["body"] == {"chat_id": "42", "text": "🔴 РОШ · диск — мало места", "disable_web_page_preview": True}
