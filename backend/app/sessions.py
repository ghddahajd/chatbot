"""in-memory хранилище сессий."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from .models import ContextFrame, Message, MessageRole, OperatorSessionSummary, Session, SessionStatus


logger = logging.getLogger(__name__)


class SessionStore:
    """простое in-memory хранилище сессий для mvp."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        # Отдельный лок на сессию — per-operation локов (self._lock) недостаточно: они защищают
        # только одну мутацию за раз, а handle_message делает несколько последовательных чтений/
        # записей (get_or_create -> append_message -> analyze_message -> remember_policy_context
        # и т.д.) по одному session_id — между этими шагами конкурентный запрос к той же сессии
        # мог вклиниться и потерять/перепутать обновления. lock_for сериализует всю обработку
        # одной сессии целиком, не трогая параллелизм между РАЗНЫМИ сессиями.
        self._session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks[session_id]

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

    async def evict_stale(self, ttl_seconds: int, operator_ttl_seconds: Optional[int] = None) -> int:
        cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
        operator_cutoff = (
            datetime.utcnow() - timedelta(seconds=operator_ttl_seconds)
            if operator_ttl_seconds is not None
            else None
        )
        operator_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}
        # Оператор держит диалог у себя, пока явно не закроет — цикл тут не "бот -> оператор ->
        # снова бот", как в колл-центрах, поэтому не трогаем эти сессии на обычном 24ч TTL.
        # Но полностью бессмертными их оставлять нельзя: если оператор потерял тикет насовсем,
        # сессия висела бы в памяти вечно — отдельный, гораздо более длинный TTL (operator_ttl_seconds)
        # служит только страховкой от заброшенных диалогов, не обычной уборкой.
        async with self._lock:
            stale_session_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if (
                    session.status not in operator_statuses
                    and session.updated_at < cutoff
                )
                or (
                    session.status in operator_statuses
                    and operator_cutoff is not None
                    and session.updated_at < operator_cutoff
                )
            ]
            for session_id in stale_session_ids:
                del self._sessions[session_id]
                self._session_locks.pop(session_id, None)
            return len(stale_session_ids)

    async def snapshot_to(
        self,
        path: Path,
        *,
        ttl_seconds: Optional[int] = None,
        operator_ttl_seconds: Optional[int] = None,
    ) -> int:
        cutoff = None
        if ttl_seconds is not None:
            cutoff = datetime.utcnow() - timedelta(seconds=ttl_seconds)
        operator_cutoff = cutoff
        if operator_ttl_seconds is not None:
            operator_cutoff = datetime.utcnow() - timedelta(seconds=operator_ttl_seconds)

        operator_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}

        def _keep(session: Session) -> bool:
            if session.status == SessionStatus.CLOSED:
                return False
            # Снапшот пишется и на плановом рестарте/деплое — здесь применяем ТОТ ЖЕ более
            # длинный operator_ttl_seconds, что и evict_stale, а не общий 24ч cutoff: иначе
            # ждущая оператора сессия старше суток молча пропадала бы при каждом деплое, хотя
            # live-эвикция (evict_stale) её бы не тронула — Telegram-карточка осталась бы висеть,
            # а привязанной сессии за ней уже не было бы ("Сессия не найдена" при попытке взять в работу).
            active_cutoff = operator_cutoff if session.status in operator_statuses else cutoff
            return active_cutoff is None or session.updated_at >= active_cutoff

        async with self._lock:
            sessions = [session for session in self._sessions.values() if _keep(session)]
            payload = [session.model_dump(mode="json") for session in sessions]

        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
        return len(payload)

    async def restore_from(self, path: Path) -> int:
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("session snapshot root must be list")
            restored = {
                session.session_id: session
                for item in payload
                if isinstance(item, dict)
                for session in [Session.model_validate(item)]
            }
        except Exception as error:
            logger.warning("session snapshot restore failed path=%s error=%s", path, type(error).__name__)
            return 0

        async with self._lock:
            self._sessions.update(restored)
        return len(restored)

    async def append_message(
        self, session_id: str, role: MessageRole, text: str, kind: Optional[str] = None
    ) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.messages.append(Message(role=role, text=text, kind=kind))
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

    async def set_telegram_bridge(
        self,
        session_id: str,
        *,
        topic_id: Optional[int] = None,
        claimed_by: Optional[str] = None,
    ) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if topic_id is not None:
                session.telegram_topic_id = topic_id
            if claimed_by is not None:
                session.telegram_claimed_by = claimed_by
            session.updated_at = datetime.utcnow()
            return session

    async def find_by_telegram_topic(self, topic_id: int) -> Optional[Session]:
        async with self._lock:
            for session in self._sessions.values():
                if session.telegram_topic_id == topic_id:
                    return session
            return None

    async def update_context(
        self,
        session_id: str,
        *,
        last_service_id: Optional[str] = None,
        last_intent: Optional[str] = None,
        active_frame: Optional[ContextFrame] = None,
        clear_active_frame: bool = False,
        substantive_message_count: Optional[int] = None,
        engagement_offer_count: Optional[int] = None,
        increment_objection_topic: Optional[str] = None,
        add_notable_flag: Optional[str] = None,
    ) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if last_service_id is not None:
                session.last_service_id = last_service_id
            if last_intent is not None:
                session.last_intent = last_intent
            if clear_active_frame:
                session.active_frame = None
            elif active_frame is not None:
                session.active_frame = active_frame
            if substantive_message_count is not None:
                session.substantive_message_count = substantive_message_count
            if engagement_offer_count is not None:
                session.engagement_offer_count = engagement_offer_count
            if increment_objection_topic:
                session.objection_response_counts[increment_objection_topic] = (
                    session.objection_response_counts.get(increment_objection_topic, 0) + 1
                )
            if add_notable_flag and add_notable_flag not in session.notable_flags:
                session.notable_flags = [*session.notable_flags, add_notable_flag]
            session.updated_at = datetime.utcnow()
            return session

    async def update_contact_draft(
        self,
        session_id: str,
        *,
        name: Optional[str] = None,
        phone: Optional[str] = None,
        metadata: Optional[dict[str, object]] = None,
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
                if metadata:
                    draft.update(metadata)
                session.contact_draft = draft
            session.updated_at = datetime.utcnow()
            return session

    async def list_operator_sessions(self, scope: str = "queue") -> list[OperatorSessionSummary]:
        async with self._lock:
            if scope == "all":
                candidates = list(self._sessions.values())
            else:
                relevant_statuses = {SessionStatus.WAITING_OPERATOR, SessionStatus.HUMAN_ACTIVE}
                candidates = [
                    session
                    for session in self._sessions.values()
                    if session.status in relevant_statuses
                ]
            sessions = [
                OperatorSessionSummary(
                    session_id=session.session_id,
                    company_id=session.company_id,
                    status=session.status,
                    last_message=session.messages[-1].text if session.messages else None,
                    updated_at=session.updated_at,
                )
                for session in candidates
            ]
            return sorted(sessions, key=lambda item: item.updated_at, reverse=True)[:200]
