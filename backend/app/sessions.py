"""in-memory хранилище сессий."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from .models import Message, MessageRole, OperatorSessionSummary, Session, SessionStatus


logger = logging.getLogger(__name__)


class SessionStore:
    """простое in-memory хранилище сессий для mvp."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, session_id: Optional[str], company_id: str) -> Session:
        async with self._lock:
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                if session.company_id == company_id:
                    return session
                logger.warning(
                    "session company mismatch session_id=%s stored_company_id=%s requested_company_id=%s",
                    session_id,
                    session.company_id,
                    company_id,
                )
                session_id = None

            session_data = {"company_id": company_id}
            if session_id:
                session_data["session_id"] = session_id
            session = Session(**session_data)
            self._sessions[session.session_id] = session
            return session

    async def get(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_all(self) -> list[Session]:
        async with self._lock:
            return list(self._sessions.values())

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

    async def set_pending_action(self, session_id: str, action: Optional[str]) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.pending_action = action
            session.updated_at = datetime.utcnow()
            return session

    async def update_context(
        self,
        session_id: str,
        *,
        last_service_id: Optional[str] = None,
        last_intent: Optional[str] = None,
    ) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if last_service_id is not None:
                session.last_service_id = last_service_id
            if last_intent is not None:
                session.last_intent = last_intent
            session.updated_at = datetime.utcnow()
            return session

    async def update_contact_draft(
        self,
        session_id: str,
        *,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        clear: bool = False,
    ) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if clear:
                session.contact_draft = {}
            else:
                draft = dict(session.contact_draft)
                if name:
                    draft["name"] = name
                if phone:
                    draft["phone"] = phone
                session.contact_draft = draft
            session.updated_at = datetime.utcnow()
            return session

    async def list_operator_sessions(self) -> list[OperatorSessionSummary]:
        async with self._lock:
            relevant_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}
            sessions = [
                OperatorSessionSummary(
                    session_id=session.session_id,
                    company_id=session.company_id,
                    status=session.status,
                    last_message=session.messages[-1].text if session.messages else None,
                    updated_at=session.updated_at,
                )
                for session in self._sessions.values()
                if session.status in relevant_statuses
            ]
            return sorted(sessions, key=lambda item: item.updated_at, reverse=True)
