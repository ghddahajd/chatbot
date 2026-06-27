"""api-роуты лидов."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from ..leads import build_lead_from_contact


router = APIRouter(prefix="/api/leads", tags=["leads"])


class LeadRequest(BaseModel):
    company_id: str = "rosh_demo"
    session_id: str
    name: str
    phone: str
    summary: str
    service_id: Optional[str] = None


@router.post("")
async def create_lead(payload: LeadRequest, request: Request) -> dict[str, bool]:
    lead_service = request.app.state.lead_service
    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(payload.company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    lead = build_lead_from_contact(
        company_id=knowledge_base.company.company_id,
        session_id=payload.session_id,
        contact={"name": payload.name, "phone": payload.phone},
        summary=payload.summary,
        service_id=payload.service_id,
    )
    await lead_service.save(lead)
    return {"ok": True}
