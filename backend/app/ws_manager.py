"""WebSocket connection manager."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from fastapi import WebSocket

from .models import SessionStatus
from .sessions import SessionStore


class ConnectionManager:
    """Tracks client and operator websockets per session."""

    def __init__(self, session_store: SessionStore) -> None:
        self.session_store = session_store
        self._client_connections: dict[str, WebSocket] = {}
        self._operator_connections: dict[str, WebSocket] = {}
        self._pending_operator_messages: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def connect_client(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._client_connections[session_id] = websocket

    async def connect_operator(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._operator_connections[session_id] = websocket
        await self.session_store.set_status(session_id, SessionStatus.HUMAN_ACTIVE)
        await self.send_to_client(
            session_id,
            {"type": "operator_joined", "text": "Специалист подключился к диалогу"},
        )

    async def disconnect_client(self, session_id: str) -> None:
        self._client_connections.pop(session_id, None)

    async def disconnect_operator(
        self,
        session_id: str,
        websocket: Optional[WebSocket] = None,
        close_session: bool = True,
    ) -> None:
        current = self._operator_connections.get(session_id)
        if websocket is not None and current is not websocket:
            return

        self._operator_connections.pop(session_id, None)
        if not close_session:
            return

        await self.session_store.set_status(session_id, SessionStatus.CLOSED)
        await self.send_to_client(
            session_id,
            {
                "type": "operator_left",
                "text": "Диалог завершён. Если остались вопросы — напишите снова.",
            },
        )

    async def send_to_client(self, session_id: str, payload: dict[str, Any]) -> None:
        websocket = self._client_connections.get(session_id)
        if websocket is None:
            self._pending_operator_messages[session_id].append(payload)
            return
        await websocket.send_json(payload)

    async def send_to_operator(self, session_id: str, payload: dict[str, Any]) -> None:
        websocket = self._operator_connections.get(session_id)
        if websocket is None:
            return
        await websocket.send_json(payload)

    async def flush_pending_to_client(self, session_id: str) -> None:
        websocket = self._client_connections.get(session_id)
        if websocket is None:
            return
        for payload in self._pending_operator_messages.pop(session_id, []):
            await websocket.send_json(payload)
