"""бот приборки: команды владельца в Telegram — отчёт как preflight, проблемы, нейросеть, сводка,
заявки без личных данных, очередь, журнал, красные линии; чужим не отвечает. И датчик SSL."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app import preflight
from app.ops_bot import COMMANDS, KEYBOARD, OpsBot, parse_command, run_ops_bot_loop
from app.utils.jsonl import append_jsonl
from app.watchdog import Watchdog

class _StopPolling(Exception):
    """останавливает бесконечный цикл опроса в тесте."""


class _Alerts:
    project = "РОШ"
    chat_id = "42"
    enabled = True

    def __init__(self, updates: list[dict] | None = None) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self.calls: list[str] = []
        self._updates = updates or []

    async def send(self, text: str, *, reply_markup: dict | None = None) -> bool:
        self.sent.append((text, reply_markup))
        return True

    async def call(self, method: str, **params):
        self.calls.append(method)
        if method == "getUpdates" and params.get("offset") == -1:
            return {"ok": True, "result": []}
        if method == "getUpdates":
            if not self._updates:
                raise _StopPolling
            batch, self._updates = self._updates, []
            return {"ok": True, "result": batch}
        return {"ok": True, "result": True}


def _reply(test_client, text: str) -> str:
    bot = OpsBot(test_client.app, _Alerts())
    return test_client.portal.call(bot.reply, text)


def _chat(test_client, message: str, session_id: str | None = None) -> dict:
    return test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message}).json()


def _open_always(test_client) -> None:
    test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company.working_hours_schedule = {}


# ---------------------------------------------------------------- команды


@pytest.mark.parametrize(
    ("text", "command"),
    [("/info", "info"), ("/info@PriborkaBot", "info"), ("Инфа", "info"), ("ПРОВЕРКА", "probes"), ("/start", "help"), ("журнал", "events"), ("что-то", None)],
)
def test_commands_by_slash_word_or_button(text: str, command) -> None:
    assert parse_command(text) == command


def test_keyboard_has_every_command() -> None:
    labels = [button["text"].lower() for row in KEYBOARD["keyboard"] for button in row]
    assert labels == [word for _slash, word, _about in COMMANDS]


def test_help_and_unknown(test_client) -> None:
    assert all(f"/{slash}" in _reply(test_client, "помощь") for slash, _word, _about in COMMANDS)
    assert "помощь" in _reply(test_client, "привет")


def test_info_is_the_preflight_report(test_client) -> None:
    text = _reply(test_client, "инфа")

    assert text.startswith("📋 РОШ — ")
    assert "нейросеть" in text and "фоновые задачи" in text
    assert "    ✅ rosh_demo — данные на месте" in text  # клиенты — вложенными строками


def test_problems_show_what_is_wrong_and_since_when(test_client, tmp_path) -> None:
    watchdog = Watchdog(alerts=_Alerts(), state_file=tmp_path / "s.json", events_file=tmp_path / "e.jsonl")
    watchdog.state["checks"]["llm_provider"] = {"status": "degraded", "bad_since": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
    test_client.app.state.watchdog = watchdog

    text = _reply(test_client, "проблемы")

    assert text.startswith("РОШ — не в порядке:")
    assert "🟠 настройка нейросети — mock mode" in text and ", 2 ч)" in text


def test_llm_check_reports_the_real_error(test_client) -> None:
    assert _reply(test_client, "нейросеть").startswith("⏭ Нейросеть не подключена")

    class _DeadKey:
        async def ping(self) -> None:
            response = httpx.Response(401, text='{"error":"The apikey has expired"}', request=httpx.Request("POST", "https://llm.test"))
            raise httpx.HTTPStatusError("401", request=response.request, response=response)

    original = test_client.app.state.llm_client
    test_client.app.state.llm_client = _DeadKey()
    try:
        assert _reply(test_client, "нейросеть") == '🔴 Нейросеть не отвечает: 401 {"error":"The apikey has expired"}'
    finally:
        test_client.app.state.llm_client = original


def test_today_counts_dialogs_and_leads(test_client) -> None:
    _open_always(test_client)
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Завтра", first["session_id"])
    _chat(test_client, "89001234567", first["session_id"])

    text = _reply(test_client, "сводка")

    today = text.split("Вчера:")[0]
    assert "· rosh_demo: диалогов 1" in today and "заявок 1" in today


def test_leads_never_show_names_or_phones(test_client) -> None:
    _open_always(test_client)
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Завтра", first["session_id"])
    _chat(test_client, "Анна 89001234567", first["session_id"])

    text = _reply(test_client, "заявки")

    assert "Запись" in text and "когда удобно: завтра" in text and "rosh_demo" in text
    assert "Анна" not in text and "900" not in text and "123" not in text


def test_queue_shows_who_waits_for_the_admin(test_client) -> None:
    _open_always(test_client)
    first = _chat(test_client, "оператор")
    _chat(test_client, "Да, менеджера", first["session_id"])

    text = _reply(test_client, "очередь")

    assert "Ждут администратора: 1 (дольше всех 1 мин)" in text


def test_events_are_readable(test_client, managed_env) -> None:
    events_file = test_client.app.state.settings.system_events_file
    append_jsonl(events_file, {"timestamp": "2026-09-24T20:13:44+00:00", "event": "started", "code": "abc"})
    append_jsonl(events_file, {"timestamp": "2026-09-24T20:13:49+00:00", "event": "problem", "check": "llm_runtime", "status": "error", "detail": "все вызовы LLM падают"})
    append_jsonl(events_file, {"timestamp": "2026-09-24T20:14:46+00:00", "event": "recovered", "check": "llm_runtime", "lasted_seconds": 57})

    text = _reply(test_client, "журнал")

    assert "24.09 23:13 🔄 запуск, код abc" in text
    assert "24.09 23:13 🔴 нейросеть: все вызовы LLM падают" in text
    assert "24.09 23:14 ✅ нейросеть — починилось (1 мин)" in text


def test_probes_run_red_lines_through_the_live_pipeline(test_client, monkeypatch) -> None:
    monkeypatch.setattr(test_client.app.state.settings, "analytics_default_company_id", "rosh_demo")

    text = _reply(test_client, "проверка")

    assert text.startswith("🧪 РОШ — красные линии (rosh_demo")
    assert "✅ кризис и самоповреждение — «self_harm_crisis»" in text
    assert len(text.splitlines()) == 1 + len(preflight.RED_LINE_PROBES)


# ---------------------------------------------------------------- безопасность опроса


def test_only_the_owner_gets_answers(test_client) -> None:
    stranger = {"update_id": 1, "message": {"chat": {"id": 777}, "text": "инфа"}}
    owner = {"update_id": 2, "message": {"chat": {"id": 42}, "text": "помощь"}}
    alerts = _Alerts(updates=[stranger, owner])

    with pytest.raises(_StopPolling):
        test_client.portal.call(run_ops_bot_loop, test_client.app, alerts)

    assert len(alerts.sent) == 1
    text, keyboard = alerts.sent[0]
    assert text.startswith("🛠 РОШ — приборка") and keyboard == KEYBOARD
    assert alerts.calls[0] == "setMyCommands"


# ---------------------------------------------------------------- SSL-сертификат


@pytest.mark.parametrize(("days", "status"), [(60, "ok"), (10, "degraded"), (2, "error")])
def test_tls_certificate_days_left(days: int, status: str, monkeypatch) -> None:
    monkeypatch.setattr(preflight, "_certificate_expires_at", lambda host, port: datetime.now(timezone.utc) + timedelta(days=days, hours=1))

    item = asyncio.run(preflight.check_tls(SimpleNamespace(public_base_url="https://roshbot.test")))

    assert item["status"] == status
    assert item["detail"] == f"roshbot.test: действует ещё {days} дн"


def test_tls_without_address_or_connection(monkeypatch) -> None:
    assert asyncio.run(preflight.check_tls(SimpleNamespace(public_base_url="")))["status"] == "skip"

    def refused(host, port):
        raise ConnectionRefusedError

    monkeypatch.setattr(preflight, "_certificate_expires_at", refused)
    item = asyncio.run(preflight.check_tls(SimpleNamespace(public_base_url="https://roshbot.test")))
    assert item["status"] == "degraded" and "ConnectionRefusedError" in item["detail"]


# ---------------------------------------------------------------- одна поломка — одно сообщение


def test_several_changes_in_one_run_come_as_one_message(tmp_path) -> None:
    alerts = _Alerts()
    watchdog = Watchdog(alerts=alerts, state_file=tmp_path / "s.json", events_file=tmp_path / "e.jsonl")
    report = {"checks": {"telegram": {"status": "error", "detail": "не ответил"}, "tls": {"status": "degraded", "detail": "10 дн"}}}
    start = datetime(2026, 9, 24, 16, 12, tzinfo=timezone.utc)

    asyncio.run(watchdog.evaluate(report, now=start))
    asyncio.run(watchdog.evaluate(report, now=start + timedelta(minutes=10)))

    assert [text for text, _markup in alerts.sent] == [
        "РОШ — сразу несколько изменений:\n"
        "🔴 Telegram — не ответил (с 24.09 19:12 МСК)\n"
        "🟠 SSL-сертификат — 10 дн (с 24.09 19:12 МСК)"
    ]
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["checks"]["tls"]["alerted"] is True
