"""websocket-роуты."""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models import MessageRole, SessionStatus


router = APIRouter(tags=["ws"])
logger = logging.getLogger(__name__)


@router.websocket("/ws/chat/{session_id}")
async def client_ws(websocket: WebSocket, session_id: str) -> None:
    session_store = websocket.app.state.session_store
    manager = websocket.app.state.ws_manager
    company_id = websocket.query_params.get("company_id")
    session = await session_store.get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    if not company_id or session.company_id != company_id:
        await websocket.close(code=4003)
        return

    await manager.connect_client(session_id, websocket)
    await manager.flush_pending_to_client(session_id)

    telegram_bridge = getattr(websocket.app.state, "telegram_bridge_service", None)

    try:
        while True:
            text = await websocket.receive_text()
            await session_store.append_message(session_id, MessageRole.USER, text)
            await manager.send_to_operator(
                session_id,
                {"type": "message", "role": "user", "text": text, "session_id": session_id},
            )
            if telegram_bridge is not None and telegram_bridge.enabled:
                try:
                    await telegram_bridge.forward_client_message(session_id, text)
                except Exception as error:
                    logger.warning(
                        "telegram_bridge forward failed session_id=%s error=%s",
                        session_id,
                        type(error).__name__,
                    )
    except WebSocketDisconnect:
        await manager.disconnect_client(session_id)


@router.websocket("/ws/operator")
async def operator_ws(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    session_id = websocket.query_params.get("session_id")
    app = websocket.app

    if token != app.state.settings.operator_token or not session_id:
        await websocket.close(code=4403)
        return

    session = await app.state.session_store.get(session_id)
    if session is None:
        await websocket.close(code=4404)
        return

    manager = app.state.ws_manager
    session_store = app.state.session_store
    await manager.connect_operator(session_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("invalid operator websocket payload session_id=%s raw=%r", session_id, raw[:200])
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            await session_store.append_message(session_id, MessageRole.OPERATOR, text)
            operator_payload = {"type": "message", "role": "operator", "text": text, "session_id": session_id}
            await manager.send_to_client(
                session_id,
                operator_payload,
            )
            await manager.send_to_operator(session_id, operator_payload)
    except WebSocketDisconnect:
        if session.status != SessionStatus.CLOSED:
            await manager.disconnect_operator(session_id, websocket=websocket)
