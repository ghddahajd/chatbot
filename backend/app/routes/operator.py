"""роуты оператора."""

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from typing import Optional

from ..auth import verify_operator_token
from ..models import SessionStatus
from ..operator import render_operator_panel


router = APIRouter(tags=["operator"])


def _company_name(request: Request, company_id: str) -> str:
    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(company_id, fallback=False)
    except KeyError:
        return company_id
    return knowledge_base.company.company_name


def _session_payload(request: Request, session) -> dict:
    payload = session.model_dump(mode="json")
    payload["company_name"] = _company_name(request, session.company_id)
    return payload


@router.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request) -> str:
    verify_operator_token(request, None)
    return render_operator_panel()


@router.get("/api/operator/sessions")
async def list_sessions(
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
    scope: str = "queue",
) -> list[dict]:
    verify_operator_token(request, x_operator_token)
    items = await request.app.state.session_store.list_operator_sessions(scope=scope)
    payloads = []
    for item in items:
        payload = item.model_dump(mode="json")
        payload["company_name"] = _company_name(request, item.company_id)
        payloads.append(payload)
    return payloads


@router.get("/api/operator/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    verify_operator_token(request, x_operator_token)
    session = await request.app.state.session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_payload(request, session)


@router.post("/api/operator/sessions/{session_id}/take")
async def take_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    verify_operator_token(request, x_operator_token)
    existing = await request.app.state.session_store.get(session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if existing.status == SessionStatus.CLOSED:
        raise HTTPException(status_code=400, detail="Session is already closed")
    if existing.status == SessionStatus.HUMAN_ACTIVE:
        return {"status": existing.status.value}

    session = await request.app.state.session_store.set_status(session_id, SessionStatus.HUMAN_ACTIVE)
    return {"status": session.status.value}


@router.post("/api/operator/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, str]:
    verify_operator_token(request, x_operator_token)
    session = await request.app.state.session_store.set_status(session_id, SessionStatus.CLOSED)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await request.app.state.ws_manager.disconnect_operator(session_id)
    return {"status": session.status.value}
