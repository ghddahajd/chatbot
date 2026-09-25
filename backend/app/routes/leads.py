"""api-роуты лидов."""

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

from ..auth import verify_operator_token
from ..leads import build_lead_from_contact


router = APIRouter(prefix="/api/leads", tags=["leads"])


class LeadRequest(BaseModel):
    company_id: str = Field(default="rosh_demo", max_length=64)
    session_id: str = Field(max_length=100)
    name: str = Field(max_length=100)
    phone: str = Field(max_length=40)
    summary: str = Field(max_length=2000)
    service_id: Optional[str] = Field(default=None, max_length=100)


@router.post("")
async def create_lead(
    payload: LeadRequest, request: Request, x_operator_token: Optional[str] = Header(default=None)
) -> dict[str, bool]:
    # заявки посетителей создаёт сам чат (chat_service); этот маршрут — только для служебных
    # проверок, открытым он позволял бы кому угодно заливать фейковые заявки
    verify_operator_token(request, x_operator_token)
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
