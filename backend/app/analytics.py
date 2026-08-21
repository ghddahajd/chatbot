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
MESSAGE_RETENTION_EVENT_TYPES = {"message_answered"}


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
        action = str((entry.get("metadata") or {}).get("action") or "unknown")
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
