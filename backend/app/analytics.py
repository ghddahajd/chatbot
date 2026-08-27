"""простая аналитика managed-service mvp."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .models import PolicyAction, PolicyReason, PolicyResult, Session
from .utils.jsonl import read_jsonl


logger = logging.getLogger(__name__)


UNKNOWN_REASONS = {
    PolicyReason.UNKNOWN_SERVICE,
    PolicyReason.PRICE_QUESTION_NO_SERVICE,
    PolicyReason.SIMILAR_SERVICES_FOUND,
}
# message_answered пишется на КАЖДЫЙ ход (track_answer) и не имеет ценности после
# отладочного окна — единственный event_type, который archive_old_analytics_events
# сворачивает в счётчики и удаляет. Остальные типы (unknown_question/regulated_handoff/
# operator_requested/...) хранятся без ограничения — там сырой текст полезен и спустя месяцы.
# widget_impression/chat_opened (2026-08-27, воронка конверсии) — та же логика: высокий
# объём (пишется на каждую загрузку страницы/каждое открытие чата), без индивидуальной
# ценности после недолгого окна — сворачиваем туда же.
MESSAGE_RETENTION_EVENT_TYPES = {"message_answered", "widget_impression", "chat_opened"}
# Воронка (conversion_funnel) джойнит эти событий по session_id, а после ретеншна сырой
# session_id пропадает (rollup хранит только счётчики) — окно воронки держим заметно
# КОРОЧЕ ретеншна (60 дней), чтобы каждая стадия всегда считалась по ещё живым сырым
# записям, а не молча деградировала на старых данных.
FUNNEL_WINDOW_DAYS = 30
WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _within_days(
    entries: list[dict[str, Any]], *, days: Optional[int], company_id: Optional[str]
) -> list[dict[str, Any]]:
    """Общий фильтр "компания + не старше N дней" — переиспользуется везде, где раньше
    каждый метод сам городил свою версию (2026-08-27, общий date-range фильтр на дашборде).
    days=None — без ограничения по дате (только company_id, если задан)."""

    cutoff = datetime.utcnow() - timedelta(days=days) if days is not None else None

    def _keep(entry: dict[str, Any]) -> bool:
        if company_id is not None and entry.get("company_id") != company_id:
            return False
        if cutoff is None:
            return True
        try:
            return datetime.fromisoformat(str(entry.get("timestamp"))) >= cutoff
        except (TypeError, ValueError):
            return False

    return [entry for entry in entries if _keep(entry)]


class AnalyticsService:
    """пишет lightweight события и строит отчёт без БД."""

    def __init__(self, analytics_file: Path, leads_file: Path) -> None:
        self.analytics_file = analytics_file
        self.leads_file = leads_file

    async def track_event(
        self,
        *,
        company_id: str,
        session_id: str,
        event_type: str,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.analytics_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.utcnow().isoformat(),
            "company_id": company_id,
            "session_id": session_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        }
        with self.analytics_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def track_answer(
        self,
        *,
        company_id: str,
        session_id: str,
        message: str,
        answer: str,
        action: str,
        policy_reason: Optional[str] = None,
    ) -> None:
        """Пишет КАЖДЫЙ обмен репликами (не только исключения), чтобы разбор "бот ответил
        ерунду" был по логу, а не по памяти или повторному прогону через LLM (она
        недетерминирована — повтор через /api/debug/trace может дать другой ответ)."""

        await self.track_event(
            company_id=company_id,
            session_id=session_id,
            event_type="message_answered",
            message=message,
            metadata={"answer": answer, "action": action, "policy_reason": policy_reason},
        )

    async def track_policy_result(
        self,
        *,
        company_id: str,
        session_id: str,
        message: str,
        policy_result: PolicyResult,
    ) -> None:
        metadata = {
            "action": policy_result.action.value,
            "reason": policy_result.reason.value,
            "service_id": policy_result.service_id,
        }
        if policy_result.reason in UNKNOWN_REASONS:
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="unknown_question",
                message=message,
                metadata=metadata,
            )
        if policy_result.reason == PolicyReason.REGULATED_ADVICE:
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="regulated_handoff",
                message=message,
                metadata=metadata,
            )
        if (
            policy_result.action == PolicyAction.TRANSFER_OPERATOR
            or policy_result.reason == PolicyReason.OPERATOR_REQUESTED
        ):
            await self.track_event(
                company_id=company_id,
                session_id=session_id,
                event_type="operator_requested",
                message=message,
                metadata=metadata,
            )

    def summary(
        self,
        sessions: list[Session],
        company_id: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        filtered_sessions = [
            session for session in sessions if company_id is None or session.company_id == company_id
        ]
        leads = [
            lead
            for lead in read_jsonl(self.leads_file)
            if company_id is None or lead.get("company_id") == company_id
        ]
        events = [
            event
            for event in read_jsonl(self.analytics_file)
            if company_id is None or event.get("company_id") == company_id
        ]

        sessions_by_status = Counter(session.status.value for session in filtered_sessions)
        sessions_by_company = Counter(session.company_id for session in filtered_sessions)
        leads_by_company = Counter(str(lead.get("company_id") or "legacy") for lead in leads)
        events_by_type = Counter(str(event.get("event_type") or "unknown") for event in events)
        events_by_company: dict[str, Counter[str]] = defaultdict(Counter)
        for event in events:
            event_company_id = str(event.get("company_id") or "unknown")
            event_type = str(event.get("event_type") or "unknown")
            events_by_company[event_company_id][event_type] += 1

        unanswered = [
            {
                "timestamp": event.get("timestamp"),
                "company_id": event.get("company_id"),
                "session_id": event.get("session_id"),
                "message": event.get("message"),
                "reason": (event.get("metadata") or {}).get("reason"),
                "service_id": (event.get("metadata") or {}).get("service_id"),
            }
            for event in reversed(events)
            if event.get("event_type") == "unknown_question"
        ][:limit]

        return {
            "company_id": company_id,
            "sessions": {
                "total": len(filtered_sessions),
                "by_status": dict(sessions_by_status),
                "by_company": dict(sessions_by_company),
                "operator_requested": sum(1 for session in filtered_sessions if session.operator_requested),
                "lead_requested": sum(1 for session in filtered_sessions if session.lead_requested),
                "messages_total": sum(len(session.messages) for session in filtered_sessions),
            },
            "leads": {
                "total": len(leads),
                "by_company": dict(leads_by_company),
            },
            "events": {
                "total": len(events),
                "by_type": dict(events_by_type),
                "by_company": {
                    event_company_id: dict(counter)
                    for event_company_id, counter in events_by_company.items()
                },
            },
            "unanswered": unanswered,
        }

    def operator_summary(self, company_id: Optional[str] = None, days: Optional[int] = None) -> dict[str, Any]:
        """Аналитика "по манагерам" (2026-08-27) — считает operator_claimed/operator_closed
        события (см. telegram_bridge._track_operator_event) и attribute'ит лиды к оператору
        через тот же session_id, ничего не меняя в самой модели Lead (лид создаётся ДО клейма,
        связи в другую сторону в данных нет — джойним тут, на чтении, не на записи).
        days — общий date-range фильтр дашборда (None = за всё время)."""

        events = _within_days(read_jsonl(self.analytics_file), days=days, company_id=company_id)
        claims = [event for event in events if event.get("event_type") == "operator_claimed"]
        closes = [event for event in events if event.get("event_type") == "operator_closed"]
        # последнее закрытие на сессию — на случай повторных /done (не должно случаться в
        # норме, но не даёт задвоить duration, если всё-таки прилетело)
        close_by_session: dict[str, dict[str, Any]] = {}
        for event in closes:
            close_by_session[str(event.get("session_id"))] = event

        leads = _within_days(read_jsonl(self.leads_file), days=days, company_id=company_id)
        operator_by_session: dict[str, str] = {
            str(claim.get("session_id")): str((claim.get("metadata") or {}).get("claimed_by") or "unknown")
            for claim in claims
        }
        leads_by_operator: Counter[str] = Counter()
        for lead in leads:
            operator = operator_by_session.get(str(lead.get("session_id")))
            if operator:
                leads_by_operator[operator] += 1

        per_operator: dict[str, dict[str, Any]] = {}
        for claim in claims:
            operator = str((claim.get("metadata") or {}).get("claimed_by") or "unknown")
            stats = per_operator.setdefault(
                operator,
                {"claimed": 0, "closed": 0, "leads": leads_by_operator.get(operator, 0), "_durations": []},
            )
            stats["claimed"] += 1
            close = close_by_session.get(str(claim.get("session_id")))
            if close is not None:
                stats["closed"] += 1
                try:
                    claimed_at = datetime.fromisoformat(str(claim.get("timestamp")))
                    closed_at = datetime.fromisoformat(str(close.get("timestamp")))
                    delta_seconds = (closed_at - claimed_at).total_seconds()
                    # Не должно случаться в норме (сессию нельзя переклеймить, не закрыв —
                    # см. session.telegram_claimed_by guard в telegram_bridge.py), но кривые
                    # данные не должны рисовать отрицательное "среднее время диалога" —
                    # это разрушает доверие к дашборду сильнее, чем просто пропуск точки.
                    if delta_seconds >= 0:
                        stats["_durations"].append(delta_seconds)
                except (TypeError, ValueError):
                    pass

        operators: dict[str, Any] = {}
        for operator, stats in per_operator.items():
            durations = stats.pop("_durations")
            stats["avg_dialog_minutes"] = round(sum(durations) / len(durations) / 60, 1) if durations else None
            operators[operator] = stats

        return {"company_id": company_id, "operators": operators}

    def leads_by_month(self, company_id: Optional[str] = None, months: int = 6) -> list[dict[str, Any]]:
        """Последние `months` календарных месяцев по счёту лидов, включая пустые (0) — иначе
        график молча пропускает провалы вместо того, чтобы их показать."""

        leads = [
            lead
            for lead in read_jsonl(self.leads_file)
            if company_id is None or lead.get("company_id") == company_id
        ]
        counts: Counter[str] = Counter()
        for lead in leads:
            month_key = str(lead.get("timestamp") or "")[:7]
            if month_key:
                counts[month_key] += 1

        now = datetime.utcnow()
        result: list[dict[str, Any]] = []
        for offset in range(months - 1, -1, -1):
            year, month_num = now.year, now.month - offset
            while month_num <= 0:
                month_num += 12
                year -= 1
            key = f"{year:04d}-{month_num:02d}"
            result.append({"month": key, "count": counts.get(key, 0)})
        return result

    def top_services(
        self, company_id: Optional[str] = None, limit: int = 8, days: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """Топ услуг по числу лидов — service_id без человекочитаемого имени (аналитика не
        знает о KnowledgeBase намеренно, имя резолвит вызывающий route, у которого он есть)."""

        leads = _within_days(read_jsonl(self.leads_file), days=days, company_id=company_id)
        counts: Counter[str] = Counter()
        for lead in leads:
            service_id = lead.get("service_id")
            if service_id:
                counts[str(service_id)] += 1
        return [{"service_id": service_id, "count": count} for service_id, count in counts.most_common(limit)]

    def leads_by_reason(self, company_id: Optional[str] = None, days: Optional[int] = None) -> list[dict[str, Any]]:
        """Разбивка лидов по reason (booking/price_question/medical_risk/commercial_interest,
        см. classify_lead_reason в leads.py) — уже есть на каждом Lead, новых полей не надо,
        для донат-чарта "какие лиды приходят"."""

        leads = _within_days(read_jsonl(self.leads_file), days=days, company_id=company_id)
        counts: Counter[str] = Counter(str(lead.get("reason") or "commercial_interest") for lead in leads)
        return [{"reason": reason, "count": count} for reason, count in counts.most_common()]

    def unanswered_trend(self, company_id: Optional[str] = None, days: int = 14) -> list[dict[str, Any]]:
        """Дневная динамика unknown_question — растёт база знаний или деградирует. Не
        подчиняется MESSAGE_RETENTION_EVENT_TYPES (хранится вечно), так что честно работает и
        для длинных окон, в отличие от unanswered_trend/conversion_funnel-класса метрик."""

        events = _within_days(read_jsonl(self.analytics_file), days=days, company_id=company_id)
        counts: Counter[str] = Counter()
        for event in events:
            if event.get("event_type") != "unknown_question":
                continue
            day_key = str(event.get("timestamp") or "")[:10]
            if day_key:
                counts[day_key] += 1

        result: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = (datetime.utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")
            result.append({"date": day, "count": counts.get(day, 0)})
        return result

    def activity_by_hour(self, company_id: Optional[str] = None, days: Optional[int] = None) -> list[dict[str, Any]]:
        """Сколько сообщений приходит в каждый час суток (UTC) — для планирования смен
        операторов. message_answered — самый частый сигнал реальной активности."""

        events = _within_days(read_jsonl(self.analytics_file), days=days, company_id=company_id)
        counts: Counter[int] = Counter()
        for event in events:
            if event.get("event_type") != "message_answered":
                continue
            try:
                hour = datetime.fromisoformat(str(event.get("timestamp"))).hour
            except (TypeError, ValueError):
                continue
            counts[hour] += 1
        return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]

    def activity_by_weekday(self, company_id: Optional[str] = None, days: Optional[int] = None) -> list[dict[str, Any]]:
        """Та же идея, но по дню недели (0=Пн ... 6=Вс) — на пару с activity_by_hour."""

        events = _within_days(read_jsonl(self.analytics_file), days=days, company_id=company_id)
        counts: Counter[int] = Counter()
        for event in events:
            if event.get("event_type") != "message_answered":
                continue
            try:
                weekday = datetime.fromisoformat(str(event.get("timestamp"))).weekday()
            except (TypeError, ValueError):
                continue
            counts[weekday] += 1
        return [
            {"weekday": weekday, "label": WEEKDAY_LABELS[weekday], "count": counts.get(weekday, 0)}
            for weekday in range(7)
        ]

    def conversion_funnel(self, company_id: Optional[str] = None, days: int = FUNNEL_WINDOW_DAYS) -> dict[str, Any]:
        """Воронка виджет-загружен → чат-открыт → есть-переписка → лид, за последние `days`
        дней (см. FUNNEL_WINDOW_DAYS — специально короче ретеншна widget_impression/
        chat_opened/message_answered, иначе стадии молча теряют сырые данные на границе окна).
        "Есть переписка" — уникальные session_id в message_answered, не отдельное событие:
        conversation is what happens when a real message gets answered, so a fresh event
        would only duplicate data already collected."""

        events = _within_days(read_jsonl(self.analytics_file), days=days, company_id=company_id)
        leads = _within_days(read_jsonl(self.leads_file), days=days, company_id=company_id)

        impressions = sum(1 for event in events if event.get("event_type") == "widget_impression")
        chat_opened = sum(1 for event in events if event.get("event_type") == "chat_opened")
        conversations = len(
            {
                event.get("session_id")
                for event in events
                if event.get("event_type") == "message_answered" and event.get("session_id")
            }
        )
        lead_count = len(leads)

        stages = [
            {"label": "Виджет загружен", "count": impressions},
            {"label": "Чат открыт", "count": chat_opened},
            {"label": "Есть переписка", "count": conversations},
            {"label": "Стал лидом", "count": lead_count},
        ]
        # шаг-к-шагу конверсия (эта стадия / предыдущая), не от общего — так и читается
        # воронка визуально: "сколько из открывших чат реально написали"
        for index, stage in enumerate(stages):
            previous = stages[index - 1]["count"] if index > 0 else None
            stage["percent_of_previous"] = (
                round(stage["count"] / previous * 100, 1) if previous else (100.0 if index == 0 else None)
            )

        return {"company_id": company_id, "days": days, "stages": stages}


def archive_old_analytics_events(analytics_file: Path, rollup_file: Path, retention_days: int) -> int:
    """удаляет записи MESSAGE_RETENTION_EVENT_TYPES старше retention_days из analytics_file,
    сохранив дневные счётчики (дата, company_id, action — без текста) в rollup_file.

    Остальные event_type не трогает — они остаются в analytics_file без ограничения.
    Возвращает количество удалённых записей. Ничего не делает (не трогает диск), если
    удалять нечего — обычный случай при ежедневном запуске.
    """

    entries = read_jsonl(analytics_file)
    if not entries:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    keep: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("event_type") not in MESSAGE_RETENTION_EVENT_TYPES:
            keep.append(entry)
            continue
        try:
            timestamp = datetime.fromisoformat(str(entry.get("timestamp")))
        except (TypeError, ValueError):
            keep.append(entry)
            continue
        (stale if timestamp < cutoff else keep).append(entry)

    if not stale:
        return 0

    rollup_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for entry in stale:
        day = str(entry.get("timestamp") or "")[:10] or "unknown"
        company_id = str(entry.get("company_id") or "unknown")
        # message_answered всегда несёт metadata.action (см. track_answer) — поведение для
        # него не меняется. widget_impression/chat_opened (2026-08-27) метаданных action не
        # имеют — раньше оба схлопнулись бы в один и тот же "unknown" рядом с message_answered
        # без action, теряя различимость; используем event_type как запасной ключ.
        action = str((entry.get("metadata") or {}).get("action") or entry.get("event_type") or "unknown")
        rollup_counts[(day, company_id, action)] += 1

    rollup_file.parent.mkdir(parents=True, exist_ok=True)
    with rollup_file.open("a", encoding="utf-8") as handle:
        for (day, company_id, action), count in sorted(rollup_counts.items()):
            handle.write(
                _dump_jsonl_line(
                    {"date": day, "company_id": company_id, "action": action, "count": count}
                )
            )

    # атомарная перезапись "горячего" файла — временный файл + rename, чтобы конкурентный
    # append (новое сообщение пришло ровно во время чистки) не попал под усечение.
    tmp_path = analytics_file.with_suffix(analytics_file.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for entry in keep:
            handle.write(_dump_jsonl_line(entry))
    os.replace(tmp_path, analytics_file)

    logger.info(
        "analytics prune: removed %d of %d message_answered entries older than %d days (analytics_file=%s)",
        len(stale),
        len(entries),
        retention_days,
        analytics_file,
    )
    return len(stale)


def _dump_jsonl_line(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False) + "\n"
