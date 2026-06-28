"""outbox-доставка лидов клиентским каналам."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx

from .knowledge import KnowledgeBaseResolver
from .leads import lead_to_payload
from .models import Lead
from .utils.jsonl import read_jsonl


logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
DELIVERY_TIMEOUT_SECONDS = 5.0


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _telegram_text(payload: dict[str, Any]) -> str:
    message = (
        "Новый лид\n"
        f"Клиент: {payload.get('company_id')}\n"
        f"Имя: {payload.get('name')}\n"
        f"Телефон: {payload.get('phone')}\n"
        f"Сессия: {payload.get('session_id')}\n"
        f"Запрос: {payload.get('summary')}"
    )
    if payload.get("service_id"):
        message += f"\nУслуга: {payload.get('service_id')}"
    return message


class DeliveryService:
    """пишет outbox и доставляет лиды в Telegram/webhook с retry metadata."""

    def __init__(
        self,
        *,
        outbox_file: Path,
        knowledge_base_resolver: KnowledgeBaseResolver,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self.outbox_file = outbox_file
        self.knowledge_base_resolver = knowledge_base_resolver
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self._lock = asyncio.Lock()

    async def enqueue_lead(self, lead: Lead) -> list[dict[str, Any]]:
        """создаёт outbox-записи и сразу делает первую попытку доставки."""

        records = []
        for destination in self._destinations_for(lead):
            record = {
                "timestamp": _iso(_utcnow()),
                "delivery_id": str(uuid4()),
                "company_id": lead.company_id,
                "session_id": lead.session_id,
                "destination_type": destination["type"],
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": _iso(_utcnow()),
                "target": destination.get("target"),
                "payload": lead_to_payload(lead),
                "last_error": None,
                "response_status": None,
            }
            await self._append_record(record)
            records.append(await self._dispatch(record))
        return records

    async def retry_due(
        self,
        *,
        company_id: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """повторяет due-доставки по текущему latest-state outbox."""

        now = _utcnow()
        attempted: list[dict[str, Any]] = []
        for record in self._latest_records().values():
            if company_id is not None and record.get("company_id") != company_id:
                continue
            if record.get("status") not in {"pending", "failed"}:
                continue
            if int(record.get("attempts") or 0) >= MAX_DELIVERY_ATTEMPTS:
                continue
            next_attempt_at = _parse_datetime(record.get("next_attempt_at"))
            if next_attempt_at is not None and next_attempt_at > now:
                continue
            attempted.append(await self._dispatch(record))
            if len(attempted) >= limit:
                break

        return {
            "attempted": len(attempted),
            "sent": sum(1 for item in attempted if item.get("status") == "sent"),
            "failed": sum(1 for item in attempted if item.get("status") == "failed"),
            "dead": sum(1 for item in attempted if item.get("status") == "dead"),
        }

    def summary(
        self,
        *,
        company_id: Optional[str] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        records = [
            record
            for record in self._latest_records().values()
            if company_id is None or record.get("company_id") == company_id
        ]
        records.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)

        by_status = Counter(str(record.get("status") or "unknown") for record in records)
        by_destination = Counter(str(record.get("destination_type") or "unknown") for record in records)
        recent = [
            {
                "timestamp": record.get("timestamp"),
                "delivery_id": record.get("delivery_id"),
                "company_id": record.get("company_id"),
                "session_id": record.get("session_id"),
                "destination_type": record.get("destination_type"),
                "status": record.get("status"),
                "attempts": record.get("attempts"),
                "next_attempt_at": record.get("next_attempt_at"),
                "last_error": record.get("last_error"),
                "response_status": record.get("response_status"),
            }
            for record in records[:limit]
        ]
        return {
            "company_id": company_id,
            "total": len(records),
            "by_status": dict(by_status),
            "by_destination": dict(by_destination),
            "recent": recent,
        }

    def _destinations_for(self, lead: Lead) -> list[dict[str, str]]:
        destinations: list[dict[str, str]] = []
        if self.telegram_bot_token and self.telegram_chat_id:
            destinations.append({"type": "telegram", "target": "telegram"})

        try:
            company = self.knowledge_base_resolver.get(lead.company_id, fallback=False).company
        except KeyError:
            logger.warning("delivery skipped webhook for unknown company_id=%s", lead.company_id)
            company = None

        if company is not None and company.lead_webhook_url:
            destinations.append({"type": "webhook", "target": company.lead_webhook_url})

        return destinations

    async def _dispatch(self, record: dict[str, Any]) -> dict[str, Any]:
        attempts = int(record.get("attempts") or 0) + 1
        status = "failed"
        response_status: Optional[int] = None
        last_error: Optional[str] = None

        try:
            response_status = await self._send(record)
            if 200 <= response_status < 300:
                status = "sent"
            else:
                last_error = f"http_status_{response_status}"
        except httpx.HTTPError as error:
            last_error = type(error).__name__
            logger.warning(
                "lead delivery failed delivery_id=%s destination=%s error=%s",
                record.get("delivery_id"),
                record.get("destination_type"),
                last_error,
            )

        if status != "sent" and attempts >= MAX_DELIVERY_ATTEMPTS:
            status = "dead"

        next_attempt_at = None
        if status == "failed":
            next_attempt_at = _iso(_utcnow() + timedelta(seconds=self._backoff_seconds(attempts)))

        updated = {
            **record,
            "timestamp": _iso(_utcnow()),
            "status": status,
            "attempts": attempts,
            "next_attempt_at": next_attempt_at,
            "last_error": last_error,
            "response_status": response_status,
        }
        await self._append_record(updated)
        return updated

    async def _send(self, record: dict[str, Any]) -> int:
        destination_type = record.get("destination_type")
        payload = record.get("payload") or {}
        delivery_id = str(record.get("delivery_id") or "")

        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            if destination_type == "telegram":
                url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                response = await client.post(
                    url,
                    json={"chat_id": self.telegram_chat_id, "text": _telegram_text(payload)},
                )
                return response.status_code

            if destination_type == "webhook":
                target = str(record.get("target") or "")
                response = await client.post(
                    target,
                    json=payload,
                    headers={
                        "X-Delivery-ID": delivery_id,
                        "X-Company-ID": str(record.get("company_id") or ""),
                    },
                )
                return response.status_code

        raise httpx.HTTPError(f"unsupported destination_type={destination_type}")

    async def _append_record(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self.outbox_file.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _latest_records(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(self.outbox_file):
            delivery_id = str(record.get("delivery_id") or "")
            if delivery_id:
                latest[delivery_id] = record
        return latest

    @staticmethod
    def _backoff_seconds(attempts: int) -> int:
        return min(3600, BASE_BACKOFF_SECONDS * (2 ** max(attempts - 1, 0)))
