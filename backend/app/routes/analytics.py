"""роуты простой аналитики managed-service mvp."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request


router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _verify_token(request: Request, header_token: Optional[str]) -> None:
    query_token = request.query_params.get("token")
    expected = request.app.state.settings.operator_token
    provided = header_token or query_token
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid operator token")


@router.get("/summary")
async def analytics_summary(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    _verify_token(request, x_operator_token)
    sessions = await request.app.state.session_store.list_all()
    return request.app.state.analytics_service.summary(
        sessions=sessions,
        company_id=company_id,
        limit=limit,
    )
