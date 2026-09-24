"""бот приборки: отвечает на команды владельца — то же, что preflight в терминале, только в Telegram.

Отвечает только чату OPS_ALERT_CHAT_ID, остальных молча игнорирует: по имени бота его найдёт кто угодно.
Всё только читает. Имён, телефонов и текстов переписки не показывает — это медицинские данные пациентов,
им место в защищённой панели. Команды слушает только сервер с OPS_COMMANDS_ENABLED (см. config.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.requests import Request

from .leads import REASON_LABELS
from .llm.mock import MockLLMClient
from .models import SessionStatus
from .ops_alerts import OpsAlerts
from .preflight import RED_LINE_PROBES, run_preflight
from .runtime_stats import STATS
from .utils.jsonl import read_jsonl
from .watchdog import LABELS, MSK, NESTED_KEYS, _duration, _is_good, _label, _msk, _parse, leaves

logger = logging.getLogger(__name__)

POLL_SECONDS = 25
LLM_CHECK_TIMEOUT_SECONDS = 20.0
LAST_LEADS = 10
LAST_EVENTS = 10
TOP_PAGES = 5
ICONS = {"ok": "✅", "skip": "⏭", "degraded": "🟠", "unavailable": "🟠", "error": "🔴"}

# (команда через «/», слово, что делает) — одно и то же можно написать или нажать кнопкой
COMMANDS = (
    ("info", "инфа", "полный отчёт, как preflight"),
    ("problems", "проблемы", "что сейчас не в порядке"),
    ("llm", "нейросеть", "живая проверка нейросети"),
    ("today", "сводка", "диалоги и заявки за сегодня и вчера"),
    ("funnel", "воронка", "воронка за 7 дней и страницы"),
    ("leads", "заявки", "последние заявки, без имён и телефонов"),
    ("queue", "очередь", "кто ждёт администратора"),
    ("events", "журнал", "что ломалось и чинилось"),
    ("probes", "проверка", "красные линии на живом классификаторе"),
    ("help", "помощь", "список команд"),
)
_BY_SLASH = {slash: slash for slash, _word, _about in COMMANDS} | {"start": "help"}
_BY_WORD = {word: slash for slash, word, _about in COMMANDS}
KEYBOARD = {
    "keyboard": [
        [{"text": COMMANDS[i][1].capitalize()}, {"text": COMMANDS[i + 1][1].capitalize()}]
        for i in range(0, len(COMMANDS), 2)
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def parse_command(text: str) -> str | None:
    value = text.strip().lower().replace("ё", "е")
    if value.startswith("/"):
        name = value[1:].split("@", 1)[0].split(maxsplit=1)
        return _BY_SLASH.get(name[0]) if name else None
    return _BY_WORD.get(value)


def _msk_day(now: datetime, days_ago: int) -> tuple[datetime, datetime]:
    """границы московского дня в том виде, как время лежит в файлах аналитики: UTC без зоны."""

    start = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)


def format_report(report: dict[str, Any], project: str) -> str:
    summary = report.get("summary") or {}
    status = str(report.get("status", "ok"))
    lines = [
        f"📋 {project} — {ICONS.get(status, '⚠️')} {'всё в порядке' if _is_good(status) else 'есть проблемы'}",
        f"в порядке {summary.get('ok', 0)} · предупреждений {summary.get('degraded', 0)} · ошибок {summary.get('error', 0)}",
        "",
    ]
    for name, item in (report.get("checks") or {}).items():
        if name == "watchdog" or not isinstance(item, dict):
            continue
        lines.append(f"{ICONS.get(str(item.get('status')), '⚠️')} {LABELS.get(name, name)} — {item.get('detail', '')}")
        for key in NESTED_KEYS:
            for entry in item.get(key) or []:
                part = entry.get("name") or entry.get("company_id") or entry.get("domain") or "?"
                detail = entry.get("detail") or ("→ " + ", ".join(entry.get("companies") or []))
                lines.append(f"    {ICONS.get(str(entry.get('status')), '⚠️')} {part} — {detail}")
    return "\n".join(lines)


class OpsBot:
    def __init__(self, app: FastAPI, alerts: OpsAlerts) -> None:
        self.app = app
        self.alerts = alerts

    @property
    def project(self) -> str:
        return self.alerts.project

    def _companies(self) -> list[str]:
        clients_dir = self.app.state.knowledge_base_resolver.clients_data_dir
        return sorted(item.name for item in clients_dir.iterdir() if item.is_dir()) if clients_dir.exists() else []

    async def reply(self, text: str) -> str:
        command = parse_command(text)
        if command is None:
            return "Не понял. Напишите «помощь» — пришлю список команд."
        try:
            return await getattr(self, f"_cmd_{command}")()
        except Exception as error:  # noqa: BLE001 — упавшая команда не должна ронять бота
            logger.warning("ops_bot command_failed command=%s error=%s", command, type(error).__name__)
            return f"⚠️ Команда «{command}» не выполнилась: {type(error).__name__}"

    async def _cmd_help(self) -> str:
        lines = [f"🛠 {self.project} — приборка. Команды (можно словом или кнопкой):"]
        lines += [f"• {word} (/{slash}) — {about}" for slash, word, about in COMMANDS]
        return "\n".join(lines)

    async def _cmd_info(self) -> str:
        return format_report(await run_preflight(self.app, include_network=True), self.project)

    async def _cmd_problems(self) -> str:
        now = datetime.now(timezone.utc)
        report = await run_preflight(self.app, include_network=True)
        bad = {name: leaf for name, leaf in leaves(report).items() if not _is_good(leaf["status"])}
        if not bad:
            return f"✅ {self.project} — проблем нет (проверено {_msk(now)} МСК)"
        watchdog = getattr(self.app.state, "watchdog", None)
        known = watchdog.state["checks"] if watchdog is not None else {}
        lines = [f"{self.project} — не в порядке:"]
        for name, leaf in bad.items():
            since = _parse((known.get(name) or {}).get("bad_since"))
            lines.append(
                f"{ICONS.get(leaf['status'], '⚠️')} {_label(name)} — {leaf['detail']}"
                + (f" (с {_msk(since)} МСК, {_duration((now - since).total_seconds())})" if since else "")
            )
        return "\n".join(lines)

    async def _cmd_llm(self) -> str:
        client = getattr(self.app.state, "llm_client", None)
        if client is None or isinstance(client, MockLLMClient):
            return "⏭ Нейросеть не подключена: работает заглушка."
        started = time.perf_counter()
        try:
            await asyncio.wait_for(client.ping(), timeout=LLM_CHECK_TIMEOUT_SECONDS)
        except httpx.HTTPStatusError as error:
            STATS.record_error("llm.ping", error)
            return f"🔴 Нейросеть не отвечает: {error.response.status_code} {error.response.text[:200]}"
        except Exception as error:  # noqa: BLE001
            STATS.record_error("llm.ping", error)
            return f"🔴 Нейросеть не отвечает: {type(error).__name__}"
        duration_ms = (time.perf_counter() - started) * 1000
        STATS.record_ok("llm.ping", duration_ms=duration_ms)
        return f"✅ Нейросеть отвечает, {int(duration_ms)} мс"

    async def _cmd_today(self) -> str:
        return await asyncio.to_thread(self._today_text, datetime.now(timezone.utc))

    def _today_text(self, now: datetime) -> str:
        analytics = self.app.state.analytics_service
        all_leads = analytics._all_leads()
        lines = [f"📊 {self.project} — сводка"]
        for title, days_ago in (("Сегодня", 0), ("Вчера", 1)):
            start, end = _msk_day(now, days_ago)
            rows = []
            for company in self._companies():
                stages = analytics.conversion_funnel(company_id=company, start=start, end=end)["stages"]
                opens, dialogs, new_leads = (stages[1]["count"], stages[2]["count"], stages[3]["count"])
                changes = sum(
                    1
                    for lead in all_leads
                    if lead.get("company_id") == company
                    and lead.get("reason") == "booking_change"
                    and (moment := _parse(lead.get("timestamp"))) is not None
                    and start <= moment.replace(tzinfo=None) <= end
                )
                if opens or dialogs or new_leads or changes:
                    row = f"· {company}: диалогов {dialogs}, открытий чата {opens}, заявок {new_leads}"
                    rows.append(row + (f", переносов и отмен {changes}" if changes else ""))
            lines.append(f"{title}:")
            lines.extend(rows or ["· ничего"])
        return "\n".join(lines)

    async def _cmd_funnel(self) -> str:
        return await asyncio.to_thread(self._funnel_text)

    def _funnel_text(self) -> str:
        analytics = self.app.state.analytics_service
        lines = [f"🔻 {self.project} — воронка за 7 дней"]
        for company in self._companies():
            funnel = analytics.conversion_funnel(company_id=company, days=7)
            counts = [stage["count"] for stage in funnel["stages"]]
            if not any(counts):
                continue
            lines.append(
                f"{company}: посетители {counts[0]} · открыли чат {counts[1]} · переписка {counts[2]} · заявки {counts[3]}"
            )
            for row in (funnel.get("pages") or [])[:TOP_PAGES]:
                lines.append(f"    {row['page']} — загрузок {row['loads']}, открытий {row['opens']}, диалогов {row['dialogs']}")
        return "\n".join(lines) if len(lines) > 1 else f"🔻 {self.project} — за 7 дней данных нет"

    async def _cmd_leads(self) -> str:
        return await asyncio.to_thread(self._leads_text)

    def _leads_text(self) -> str:
        resolver = self.app.state.knowledge_base_resolver
        leads = sorted(self.app.state.analytics_service._all_leads(), key=lambda lead: str(lead.get("timestamp") or ""), reverse=True)
        if not leads:
            return f"📝 {self.project} — заявок пока нет"
        lines = [f"📝 {self.project} — последние заявки (без имён и телефонов)"]
        for lead in leads[:LAST_LEADS]:
            moment = _parse(lead.get("timestamp"))
            company = str(lead.get("company_id") or "?")
            service = None
            try:
                service = resolver.get(company, fallback=False).find_service_by_id(lead.get("service_id"))
            except Exception:  # noqa: BLE001 — клиента могли убрать, заявка всё равно показывается
                service = None
            parts = [
                _msk(moment) if moment else "?",
                REASON_LABELS.get(str(lead.get("reason") or ""), str(lead.get("reason") or "")),
                service.name if service else "",
                f"когда удобно: {lead['preferred_time']}" if lead.get("preferred_time") else "",
                company,
            ]
            lines.append("· " + " · ".join(part for part in parts if part))
        return "\n".join(lines)

    async def _cmd_queue(self) -> str:
        now = datetime.utcnow()
        sessions = await self.app.state.session_store.list_all()
        waiting = [session for session in sessions if session.status == SessionStatus.WAITING_OPERATOR]
        human = [session for session in sessions if session.status == SessionStatus.HUMAN_ACTIVE]
        waits = []
        for session in waiting:
            handoff = next((item.created_at for item in reversed(session.messages) if item.kind == "handoff"), session.updated_at)
            waits.append((now - handoff).total_seconds())
        bridge = getattr(self.app.state, "telegram_bridge_service", None)
        pending = bridge.pending_count() if bridge is not None else 0
        return "\n".join(
            [
                f"👥 {self.project} — очередь",
                f"Ждут администратора: {len(waiting)}" + (f" (дольше всех {_duration(max(waits))})" if waits else ""),
                f"Ведёт администратор: {len(human)}",
                f"Недосланных карточек в Telegram: {pending}",
            ]
        )

    async def _cmd_events(self) -> str:
        events = read_jsonl(self.app.state.settings.system_events_file)[-LAST_EVENTS:]
        if not events:
            return f"📜 {self.project} — журнал пуст"
        lines = [f"📜 {self.project} — последние события"]
        for event in events:
            moment = _parse(event.get("timestamp"))
            when = _msk(moment) if moment else "?"
            kind = event.get("event")
            if kind == "started":
                lines.append(f"{when} 🔄 запуск, код {event.get('code', '?')}")
            elif kind == "problem":
                lines.append(f"{when} {ICONS.get(str(event.get('status')), '⚠️')} {_label(str(event.get('check')))}: {str(event.get('detail', ''))[:100]}")
            elif kind == "recovered":
                lines.append(f"{when} ✅ {_label(str(event.get('check')))} — починилось ({_duration(float(event.get('lasted_seconds') or 0))})")
        return "\n".join(lines)

    async def _cmd_probes(self) -> str:
        from .routes.debug import DebugTraceRequest, debug_trace  # поздний импорт: роуты тянут всё приложение

        settings = self.app.state.settings
        company = settings.analytics_default_company_id
        lines = [f"🧪 {self.project} — красные линии ({company}, живой классификатор)"]
        for title, message, expected, critical in RED_LINE_PROBES:
            request = Request({"type": "http", "app": self.app, "method": "POST", "path": "/api/debug/trace", "headers": [], "query_string": b""})
            try:
                trace = await debug_trace(DebugTraceRequest(company_id=company, message=message), request, x_operator_token=settings.operator_token)
            except Exception as error:  # noqa: BLE001
                lines.append(f"🔴 {title} — проверка упала: {type(error).__name__}")
                continue
            decision = next((step["result"] for step in trace.get("steps", []) if step.get("step") == "policy_decision"), {})
            reason = str(decision.get("reason") or "")
            icon = "✅" if reason in expected else ("🔴" if critical else "🟠")
            lines.append(f"{icon} {title} — «{reason}»" + ("" if reason in expected else f", ожидали {'/'.join(sorted(expected))}"))
        return "\n".join(lines)


async def run_ops_bot_loop(app: FastAPI, alerts: OpsAlerts) -> None:
    bot = OpsBot(app, alerts)
    await alerts.call("setMyCommands", commands=[{"command": slash, "description": about} for slash, _word, about in COMMANDS])
    offset: int | None = None
    # то, что накопилось, пока сервер лежал, пропускаем: ответы на старые команды только путали бы
    backlog = await alerts.call("getUpdates", offset=-1, timeout=0)
    if backlog and backlog.get("result"):
        offset = int(backlog["result"][-1]["update_id"]) + 1
    while True:
        params: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        data = await alerts.call("getUpdates", _http_timeout=POLL_SECONDS + 15, **params)
        if data is None:
            await asyncio.sleep(5)
            continue
        for update in data.get("result") or []:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            text = message.get("text")
            if str((message.get("chat") or {}).get("id") or "") != alerts.chat_id or not isinstance(text, str):
                continue
            await alerts.send(await bot.reply(text), reply_markup=KEYBOARD)
