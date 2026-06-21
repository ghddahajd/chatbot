"""api-роуты лидов."""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Optional

from ..leads import build_lead_from_contact


router = APIRouter(prefix="/api/leads", tags=["leads"])


class LeadRequest(BaseModel):
    session_id: str
    name: str
    phone: str
    summary: str
    service_id: Optional[str] = None


@router.post("")
async def create_lead(payload: LeadRequest, request: Request) -> dict[str, bool]:
    lead_service = request.app.state.lead_service
    lead = build_lead_from_contact(
        session_id=payload.session_id,
        contact={"name": payload.name, "phone": payload.phone},
        summary=payload.summary,
        service_id=payload.service_id,
    )
    await lead_service.save(lead)
    return {"ok": True}
