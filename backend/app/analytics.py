"""простая аналитика managed-service mvp."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .models import PolicyAction, PolicyReason, PolicyResult, Session
from .utils.jsonl import read_jsonl


UNKNOWN_REASONS = {
    PolicyReason.UNKNOWN_SERVICE,
    PolicyReason.PRICE_QUESTION_NO_SERVICE,
    PolicyReason.SIMILAR_SERVICES_FOUND,
}


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
