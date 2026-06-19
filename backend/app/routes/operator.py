"""Operator routes."""

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from typing import Optional

from ..models import SessionStatus
from ..operator import render_operator_panel


router = APIRouter(tags=["operator"])


def _verify_token(request: Request, header_token: Optional[str]) -> None:
    query_token = request.query_params.get("token")
    expected = request.app.state.settings.operator_token
    provided = header_token or query_token
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid operator token")


@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request) -> str:
    _verify_token(request, None)
    return render_operator_panel()


@router.get("/api/operator/sessions")
async def list_sessions(
    request: Request, x_operator_token: Optional[str] = Header(default=None)
) -> list[dict]:
    _verify_token(request, x_operator_token)
    items = await request.app.state.session_store.list_operator_sessions()
    return [item.model_dump(mode="json") for item in items]


@router.get("/api/operator/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _verify_token(request, x_operator_token)
    session = await request.app.state.session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.model_dump(mode="json")


@router.post("/api/operator/sessions/{session_id}/take")
async def take_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    _verify_token(request, x_operator_token)
    session = await request.app.state.session_store.set_status(session_id, SessionStatus.HUMAN_ACTIVE)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": session.status.value}


@router.post("/api/operator/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    _verify_token(request, x_operator_token)
    session = await request.app.state.session_store.set_status(session_id, SessionStatus.CLOSED)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await request.app.state.ws_manager.disconnect_operator(session_id)
    return {"status": session.status.value}
