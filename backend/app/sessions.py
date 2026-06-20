"""In-memory session store."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from .models import Message, MessageRole, OperatorSessionSummary, Session, SessionStatus


class SessionStore:
    """Simple in-memory session storage for MVP."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, session_id: Optional[str], company_id: str) -> Session:
        async with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]

            session_data = {"company_id": company_id}
            if session_id:
                session_data["session_id"] = session_id
            session = Session(**session_data)
            self._sessions[session.session_id] = session
            return session

    async def get(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def append_message(self, session_id: str, role: MessageRole, text: str) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.messages.append(Message(role=role, text=text))
            if role == MessageRole.USER:
                session.message_count += 1
            session.updated_at = datetime.utcnow()
            return session

    async def set_status(self, session_id: str, status: SessionStatus) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.status = status
            session.updated_at = datetime.utcnow()
            return session

    async def set_lead_requested(self, session_id: str, value: bool = True) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.lead_requested = value
            session.updated_at = datetime.utcnow()
            return session

    async def set_operator_requested(self, session_id: str, value: bool = True) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.operator_requested = value
            session.updated_at = datetime.utcnow()
            return session

    async def list_operator_sessions(self) -> list[OperatorSessionSummary]:
        async with self._lock:
            relevant_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}
            sessions = [
                OperatorSessionSummary(
                    session_id=session.session_id,
                    status=session.status,
                    last_message=session.messages[-1].text if session.messages else None,
                    updated_at=session.updated_at,
                )
                for session in self._sessions.values()
                if session.status in relevant_statuses
            ]
            return sorted(sessions, key=lambda item: item.updated_at, reverse=True)
