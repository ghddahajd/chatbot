"""роуты простой аналитики managed-service mvp."""

from typing import Optional

from fastapi import APIRouter, Header, Query, Request

from ..auth import verify_operator_token


router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    verify_operator_token(request, x_operator_token)
    sessions = await request.app.state.session_store.list_all()
    return request.app.state.analytics_service.summary(
        sessions=sessions,
        company_id=company_id,
        limit=limit,
    )
