"""сохранение лидов и опциональные уведомления."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .models import Lead
from .utils.jsonl import append_jsonl

if TYPE_CHECKING:
    from .delivery import DeliveryService


logger = logging.getLogger(__name__)


def lead_to_payload(lead: Lead) -> dict[str, Any]:
    return {
        "timestamp": lead.timestamp.isoformat(),
        "company_id": lead.company_id,
        "session_id": lead.session_id,
        "name": lead.name,
        "phone": lead.phone,
        "summary": lead.summary,
        "service_id": lead.service_id,
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
) -> Lead:
    """создаёт объект лида из распарсенных контактных данных."""

    return Lead(
        company_id=company_id,
        session_id=session_id,
        name=str(contact.get("name") or "Не указано"),
        phone=str(contact.get("phone") or ""),
        summary=summary,
        service_id=service_id,
    )
