"""сохранение лидов и опциональные уведомления."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .models import Lead, Session
from .utils.jsonl import append_jsonl

if TYPE_CHECKING:
    from .delivery import DeliveryService


logger = logging.getLogger(__name__)

MEDICAL_RISK_INTENTS = {"regulated_advice", "medical_advice"}
REASON_LABELS = {
    "booking": "Запись",
    "price_question": "Цена",
    "medical_risk": "Консультация",
    "commercial_interest": "Оператор/контакт",
}


def classify_lead_reason(*, last_intent: Optional[str], is_booking_request: bool) -> str:
    """детерминированно сводит уже посчитанные policy-сигналы, не классифицирует заново.

    `last_intent` должен быть значением ДО того, как текущее (контактное) сообщение
    перезаписало `session.last_intent` — иначе reason всегда будет "contact_provided"
    вместо реальной причины (bug: сессия мутируется до построения лида в основном потоке).
    """

    if is_booking_request:
        return "booking"
    if last_intent in MEDICAL_RISK_INTENTS:
        return "medical_risk"
    if last_intent == "price_question":
        return "price_question"
    return "commercial_interest"


def lead_trigger_for(*, is_booking_request: bool, is_operator_flow: bool) -> str:
    if is_booking_request:
        return "booking_request"
    if is_operator_flow:
        return "operator_handoff"
    return "ask_contact"


def recent_messages_for(session: Session, limit: int = 8) -> list[dict[str, str]]:
    return [
        {"role": message.role.value, "text": message.text}
        for message in session.messages[-limit:]
    ]


def lead_to_payload(lead: Lead) -> dict[str, Any]:
    return {
        "timestamp": lead.timestamp.isoformat(),
        "company_id": lead.company_id,
        "session_id": lead.session_id,
        "name": lead.name,
        "phone": lead.phone,
        "summary": lead.summary,
        "service_id": lead.service_id,
        "reason": lead.reason,
        "needs_operator": lead.needs_operator,
        "lead_trigger": lead.lead_trigger,
        "recent_messages": lead.recent_messages,
        "operator_url": lead.operator_url,
    }


class LeadService:
    """сохраняет лиды локально и ставит доставку в outbox."""

    def __init__(
        self,
        leads_file: Path,
        delivery_service: Optional[DeliveryService] = None,
    ) -> None:
        self.leads_file = leads_file
        self.delivery_service = delivery_service

    async def save(self, lead: Lead, event_type: str = "lead_created") -> None:
        append_jsonl(self.leads_file, lead_to_payload(lead))

        if self.delivery_service is not None:
            try:
                await self.delivery_service.enqueue_event(
                    event_type=event_type,
                    company_id=lead.company_id,
                    session_id=lead.session_id,
                    payload=lead_to_payload(lead),
                )
            except Exception as error:
                logger.warning(
                    "lead delivery enqueue failed company_id=%s session_id=%s event_type=%s error=%s",
                    lead.company_id,
                    lead.session_id,
                    event_type,
                    type(error).__name__,
                )


def build_lead_from_contact(
    company_id: str,
    session_id: str,
    contact: dict[str, Any],
    summary: str,
    service_id: Optional[str] = None,
    *,
    reason: str = "commercial_interest",
    needs_operator: bool = False,
    lead_trigger: str = "ask_contact",
    recent_messages: Optional[list[dict[str, str]]] = None,
    operator_url: str = "",
) -> Lead:
    """создаёт объект лида из распарсенных контактных данных."""

    return Lead(
        company_id=company_id,
        session_id=session_id,
        name=str(contact.get("name") or "Не указано"),
        phone=str(contact.get("phone") or ""),
        summary=summary,
        service_id=service_id,
        reason=reason,
        needs_operator=needs_operator,
        lead_trigger=lead_trigger,
        recent_messages=recent_messages or [],
        operator_url=operator_url,
    )
