"""сохранение лидов и опциональные уведомления."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from .models import Lead


logger = logging.getLogger(__name__)


def _lead_to_payload(lead: Lead) -> dict[str, Any]:
    return {
        "timestamp": lead.timestamp.isoformat(),
        "session_id": lead.session_id,
        "name": lead.name,
        "phone": lead.phone,
        "summary": lead.summary,
        "service_id": lead.service_id,
    }


class LeadService:
    """сохраняет лиды локально и опционально отправляет их в telegram."""

    def __init__(
        self,
        leads_file: Path,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self.leads_file = leads_file
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id

    async def save(self, lead: Lead) -> None:
        self.leads_file.parent.mkdir(parents=True, exist_ok=True)
        with self.leads_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_lead_to_payload(lead), ensure_ascii=False) + "\n")

        if self.telegram_bot_token and self.telegram_chat_id:
            await self._send_telegram(lead)

    async def _send_telegram(self, lead: Lead) -> None:
        message = (
            "Новый лид\n"
            f"Имя: {lead.name}\n"
            f"Телефон: {lead.phone}\n"
            f"Сессия: {lead.session_id}\n"
            f"Запрос: {lead.summary}"
        )
        if lead.service_id:
            message += f"\nУслуга: {lead.service_id}"

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": message}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except httpx.HTTPError as error:
            logger.warning("telegram lead notification failed error=%s", type(error).__name__)
            return


def build_lead_from_contact(
    session_id: str,
    contact: dict[str, Any],
    summary: str,
    service_id: Optional[str] = None,
) -> Lead:
    """создаёт объект лида из распарсенных контактных данных."""

    return Lead(
        session_id=session_id,
        name=str(contact.get("name") or "Не указано"),
        phone=str(contact.get("phone") or ""),
        summary=summary,
        service_id=service_id,
    )
