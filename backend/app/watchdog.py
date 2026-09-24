"""сторож: сам гоняет проверки preflight и сообщает нам о поломках и починках.

Раньше preflight запускали только руками при выкатке, и сбой между выкатками мог молчать днями
(нейросеть без ключа 20–24.09 так и не заметили). Здесь раз в N минут:
- если за последний час не было ни одного вызова нейросети — одна крошечная проверка, иначе
  ночью без трафика сломанный ключ не виден;
- прогон тех же проверок, что preflight; у каждой запоминаем «в порядке / проблема» и с какого момента;
- оповещение, когда проблема продержалась confirm_runs прогонов подряд, напоминание раз в
  reminder_hours, «починилось» с длительностью; каждая смена состояния — строка в журнал событий;
- раз в сутки утренняя сводка: заодно страховка от тишины — нет сводки, значит лёг сам сторож.

Состояние переживает перезапуск (файл), поэтому после выкатки те же проблемы не приходят заново.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .llm.mock import MockLLMClient
from .ops_alerts import OpsAlerts
from .preflight import code_fingerprint, run_preflight
from .runtime_stats import STATS
from .utils.jsonl import append_jsonl, read_jsonl

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
LLM_PING_TIMEOUT_SECONDS = 20.0
GOOD_STATUSES = {"ok", "skip"}
NESTED_KEYS = ("clients", "tasks", "domains")
# сам /health показывает вердикт сторожа — его не оцениваем, иначе сторож тревожился бы о себе
IGNORED_CHECKS = {"watchdog"}
LABELS = {
    "llm_runtime": "нейросеть",
    "llm_provider": "настройка нейросети",
    "telegram": "Telegram",
    "delivery": "доставка заявок в CRM",
    "storage": "диск",
    "resources": "память и папка настроек",
    "background_tasks": "фоновые задачи",
    "tasks": "фоновая задача",
    "clients": "данные клиента",
    "knowledge_base": "база знаний",
    "rag_index": "статьи",
    "domains": "домены",
    "cors": "доступ сайтов к виджету",
    "leads": "запись заявок",
    "analytics": "аналитика",
    "logging": "логи",
    "operator_token": "токен оператора",
    "sessions": "сессии",
    "app": "приложение",
    "tls": "SSL-сертификат",
}


def _label(name: str) -> str:
    check, _, part = name.partition(":")
    base = LABELS.get(check, check)
    return f"{base} {part}" if part else base


def _is_good(status: str | None) -> bool:
    return str(status or "ok") in GOOD_STATUSES


def _icon(status: str) -> str:
    return "🔴" if status == "error" else "🟠"


def _duration(seconds: float) -> str:
    minutes = max(int(seconds // 60), 1)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


def _msk(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m %H:%M")


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def leaves(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    """плоский список проверок: вложенные (клиенты, задачи, домены) — по отдельности, а общий
    пункт — только если он в беде сам по себе, иначе одна поломка пришла бы двумя сообщениями."""

    result: dict[str, dict[str, str]] = {}
    for name, item in (report.get("checks") or {}).items():
        if name in IGNORED_CHECKS or not isinstance(item, dict):
            continue
        nested = [entry for key in NESTED_KEYS for entry in (item.get(key) or []) if isinstance(entry, dict)]
        nested_bad = False
        for entry in nested:
            part = entry.get("name") or entry.get("company_id") or entry.get("domain") or "?"
            leaf = "tasks" if name == "background_tasks" else name
            result[f"{leaf}:{part}"] = {"status": str(entry.get("status", "ok")), "detail": str(entry.get("detail", ""))}
            nested_bad = nested_bad or not _is_good(entry.get("status"))
        if not nested or (not nested_bad and not _is_good(item.get("status"))):
            result[name] = {"status": str(item.get("status", "ok")), "detail": str(item.get("detail", ""))}
    return result


class Watchdog:
    def __init__(
        self,
        *,
        alerts: OpsAlerts,
        state_file: Path,
        events_file: Path,
        confirm_runs: int = 2,
        reminder_hours: float = 6,
    ) -> None:
        self.alerts = alerts
        self.state_file = state_file
        self.events_file = events_file
        self.confirm_runs = max(confirm_runs, 1)
        self.reminder_seconds = reminder_hours * 3600
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"checks": {}}
        return data if isinstance(data, dict) and isinstance(data.get("checks"), dict) else {"checks": {}}

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_file.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self.state_file)
        except OSError as error:
            logger.warning("watchdog state_save_failed error=%s", type(error).__name__)

    def _event(self, now: datetime, kind: str, **fields: Any) -> None:
        try:
            append_jsonl(self.events_file, {"timestamp": now.isoformat(), "event": kind, **fields})
        except OSError as error:
            logger.warning("watchdog event_write_failed error=%s", type(error).__name__)

    def problems(self) -> list[str]:
        """подтверждённые проблемы: продержались confirm_runs прогонов, разовый сбой сюда не попадает."""

        return sorted(
            name
            for name, item in self.state["checks"].items()
            if not _is_good(item.get("status")) and int(item.get("bad_runs", 0)) >= self.confirm_runs
        )

    async def evaluate(self, report: dict[str, Any], now: datetime | None = None) -> list[str]:
        """сравнивает свежий отчёт с прошлым состоянием; всё, что изменилось за прогон, — одним
        сообщением (лёг прокси — это и Telegram, и фоновая задача, не два сигнала). Возвращает отправленное."""

        now = now or datetime.now(timezone.utc)
        lines: list[str] = []
        to_mark: list[dict[str, Any]] = []  # проблемы, которые после отправки считаются сообщёнными
        current = leaves(report)
        checks: dict[str, Any] = self.state["checks"]
        for name in list(checks):
            if name not in current:  # клиента или домен убрали — забываем молча
                checks.pop(name)
        for name, leaf in current.items():
            status, detail = leaf["status"], leaf["detail"]
            prev = checks.get(name)
            if _is_good(status):
                if prev and not _is_good(prev.get("status")):
                    bad_since = _parse(prev.get("bad_since")) or now
                    self._event(now, "recovered", check=name, was=prev.get("status"), lasted_seconds=int((now - bad_since).total_seconds()))
                    if prev.get("alerted"):
                        lines.append(f"✅ {_label(name)} — снова в порядке, проблема длилась {_duration((now - bad_since).total_seconds())}")
                checks[name] = {"status": status, "detail": detail}
                continue

            if prev is None or _is_good(prev.get("status")):
                self._event(now, "problem", check=name, status=status, detail=detail)
                entry = {"status": status, "detail": detail, "bad_since": now.isoformat(), "bad_runs": 1, "alerted": False}
            else:
                entry = {**prev, "status": status, "detail": detail, "bad_runs": int(prev.get("bad_runs", 0)) + 1}
            bad_since = _parse(entry.get("bad_since")) or now
            last_alert = _parse(entry.get("last_alert_at"))
            if not entry.get("alerted") and entry["bad_runs"] >= self.confirm_runs:
                lines.append(f"{_icon(status)} {_label(name)} — {detail} (с {_msk(bad_since)} МСК)")
                to_mark.append(entry)
            elif entry.get("alerted") and last_alert and (now - last_alert).total_seconds() >= self.reminder_seconds:
                lines.append(f"{_icon(status)} {_label(name)} — всё ещё не в порядке, уже {_duration((now - bad_since).total_seconds())}: {detail}")
                to_mark.append(entry)
            checks[name] = entry

        sent: list[str] = []
        if lines:
            project = self.alerts.project
            if len(lines) == 1:
                icon, _, rest = lines[0].partition(" ")
                text = f"{icon} {project} · {rest}"
            else:
                text = f"{project} — сразу несколько изменений:\n" + "\n".join(lines)
            if await self.alerts.send(text):
                sent.append(text)
                for entry in to_mark:
                    entry["alerted"] = True
                    entry["last_alert_at"] = now.isoformat()
        self._save()
        return sent

    async def started(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._event(now, "started", code=code_fingerprint())
        await self.alerts.send(f"🔄 {self.alerts.project} — сервер запущен, код {code_fingerprint()}")

    def digest_due(self, now: datetime | None = None, *, hour_msk: int = 10) -> bool:
        """раз в сутки после hour_msk; если сторож лежал утром — сводка придёт при первом прогоне после."""

        local = (now or datetime.now(timezone.utc)).astimezone(MSK)
        return local.hour >= hour_msk and self.state.get("last_digest_date") != local.date().isoformat()

    async def send_digest(self, summary_lines: list[str], now: datetime | None = None) -> str | None:
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(MSK).date().isoformat()
        problems = self.problems()
        status_line = (
            "Всё в порядке."
            if not problems
            else "Проблемы сейчас: " + ", ".join(
                f"{_label(name)} (с {_msk(_parse(self.state['checks'][name].get('bad_since')) or now)})" for name in problems
            )
        )
        text = "\n".join([f"☀️ {self.alerts.project} — утренняя сводка", status_line, *summary_lines])
        if not self.alerts.enabled or await self.alerts.send(text):
            self.state["last_digest_date"] = today
            self._save()
            return text
        return None


def yesterday_activity(analytics_file: Path, leads: list[dict[str, Any]], now: datetime | None = None) -> list[str]:
    """диалоги и заявки за вчера по московскому дню, по каждому клиенту, где что-то было."""

    now = now or datetime.now(timezone.utc)
    today_start = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today_start - timedelta(days=1), today_start

    def _in_day(entry: dict[str, Any]) -> bool:
        moment = _parse(entry.get("timestamp"))
        return moment is not None and start <= moment < end

    dialogs: dict[str, set[str]] = {}
    for event in read_jsonl(analytics_file):
        if event.get("event_type") == "message_answered" and event.get("session_id") and _in_day(event):
            dialogs.setdefault(str(event.get("company_id") or "?"), set()).add(str(event["session_id"]))
    new_leads: dict[str, int] = {}
    changes: dict[str, int] = {}
    for lead in leads:
        if not _in_day(lead):
            continue
        company = str(lead.get("company_id") or "?")
        bucket = changes if lead.get("reason") == "booking_change" else new_leads
        bucket[company] = bucket.get(company, 0) + 1
    companies = sorted(set(dialogs) | set(new_leads) | set(changes))
    if not companies:
        return ["Вчера диалогов не было."]
    lines = ["Вчера:"]
    for company in companies:
        line = f"· {company}: диалогов {len(dialogs.get(company, ()))}, заявок {new_leads.get(company, 0)}"
        if changes.get(company):
            line += f", переносов и отмен {changes[company]}"
        lines.append(line)
    return lines


async def ping_llm_if_idle(app: FastAPI) -> None:
    client = getattr(app.state, "llm_client", None)
    if client is None or isinstance(client, MockLLMClient):
        return
    calls = STATS.summary("llm.")
    if sum(item["ok"] + item["errors"] for item in calls.values()):
        return
    started = time.perf_counter()
    try:
        await asyncio.wait_for(client.ping(), timeout=LLM_PING_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 — любая ошибка и есть результат проверки
        STATS.record_error("llm.ping", error)
        return
    STATS.record_ok("llm.ping", duration_ms=(time.perf_counter() - started) * 1000)


def ops_alerts_from_settings(settings: Any) -> OpsAlerts:
    return OpsAlerts(
        bot_token=settings.ops_alert_bot_token,
        chat_id=settings.ops_alert_chat_id,
        project=settings.ops_project_name,
        proxy_url=settings.telegram_proxy_url,
    )


async def run_watchdog_loop(app: FastAPI) -> None:
    settings = app.state.settings
    watchdog = Watchdog(
        alerts=ops_alerts_from_settings(settings),
        state_file=settings.watchdog_state_file,
        events_file=settings.system_events_file,
        confirm_runs=settings.watchdog_confirm_runs,
        reminder_hours=settings.watchdog_reminder_hours,
    )
    app.state.watchdog = watchdog
    await asyncio.sleep(settings.watchdog_first_delay_seconds)
    first = True
    while True:
        try:
            if first:
                await watchdog.started()
                first = False
            await ping_llm_if_idle(app)
            report = await run_preflight(app, include_network=True)
            await watchdog.evaluate(report)
            app.state.watchdog_status = {"problems": watchdog.problems(), "checked_at": datetime.now(timezone.utc).isoformat()}
            if watchdog.digest_due(hour_msk=settings.ops_digest_hour_msk):
                leads = await asyncio.to_thread(app.state.analytics_service._all_leads)
                await watchdog.send_digest(await asyncio.to_thread(yesterday_activity, settings.analytics_file, leads))
        except Exception as error:  # noqa: BLE001 — один неудачный прогон не должен останавливать сторожа
            logger.warning("watchdog run_failed error=%s", type(error).__name__)
        await asyncio.sleep(settings.watchdog_interval_seconds)
