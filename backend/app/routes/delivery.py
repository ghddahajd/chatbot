"""админские роуты delivery outbox."""

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request


router = APIRouter(prefix="/api/delivery", tags=["delivery"])


def _verify_token(request: Request, header_token: Optional[str]) -> None:
    query_token = request.query_params.get("token")
    expected = request.app.state.settings.operator_token
    provided = header_token or query_token
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid operator token")


@router.get("/outbox")
async def delivery_outbox(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_token(request, x_operator_token)
    return request.app.state.delivery_service.summary(company_id=company_id, limit=limit)


@router.post("/retry")
async def retry_delivery(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_token(request, x_operator_token)
    return await request.app.state.delivery_service.retry_due(company_id=company_id, limit=limit)
