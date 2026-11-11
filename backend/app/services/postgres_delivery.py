"""SQL-очередь: транзакции заканчиваются до HTTP, отправки выполняет retry worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4, uuid5, NAMESPACE_URL

from ..delivery import (
    DeliveryService, MAX_DELIVERY_ATTEMPTS, _iso, _utcnow,
)
from ..leads import lead_to_payload
from ..models import Lead
from ..storage.errors import LeadStorageError
from ..storage.request_context import prepare_lead_receipt, mark_lead_committed

logger = logging.getLogger(__name__)
TELEGRAM_LEAD_CARD = "telegram_lead_card"


class PostgresDeliveryService(DeliveryService):
    def __init__(self, *, store, knowledge_base_resolver, telegram_bot_token="",
                 telegram_chat_id="", telegram_dm_enabled=False):
        self.store = store
        self.knowledge_base_resolver = knowledge_base_resolver
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.telegram_dm_enabled = telegram_dm_enabled
        self.telegram_bridge = None
        self._wakeup = asyncio.Event()

    def prepare_event(self, *, event_type, company_id, session_id, payload):
        self.knowledge_base_resolver.get(company_id, fallback=False)
        return [{
            "timestamp": _iso(_utcnow()), "delivery_id": str(uuid4()), "event_type": event_type,
            "company_id": company_id, "session_id": session_id, "destination_type": destination["type"],
            "status": "pending", "attempts": 0, "next_attempt_at": _iso(_utcnow()),
            "target": destination.get("target"), "payload": payload, "last_error": None,
            "response_status": None,
        } for destination in self._destinations_for(company_id=company_id, event_type=event_type)]

    async def enqueue_event(self, *, event_type, company_id, session_id, payload):
        try:
            records = self.prepare_event(event_type=event_type, company_id=company_id,
                                         session_id=session_id, payload=payload)
            await self.store.enqueue(records)
        except Exception as error:
            logger.error("delivery enqueue failed error=%s", type(error).__name__)
            raise LeadStorageError("Не удалось сохранить отправку") from None
        self._wakeup.set()
        return records

    async def retry_due(self, *, company_id=None, limit=50):
        try:
            return await self._retry_due(company_id=company_id, limit=limit)
        except Exception as error:
            logger.error("delivery processing failed error=%s", type(error).__name__)
            raise LeadStorageError("Обработка очереди недоступна") from None

    async def _retry_due(self, *, company_id=None, limit=50):
        attempted = []
        for _ in range(max(0, limit)):
            # Не берём пачку в аренду: последовательные HTTP-запросы могут пережить её срок.
            records = await self.store.claim(company_id=company_id, limit=1)
            if not records:
                break
            attempted.append(await self._dispatch_claimed(records[0]))
        return {"attempted": len(attempted), **{
            status: sum(item["status"] == status for item in attempted)
            for status in ("sent", "failed", "dead")
        }}

    async def _dispatch_claimed(self, record: dict[str, Any]) -> dict[str, Any]:
        status, response_status, last_error = "failed", None, None
        try:
            response_status = await self._send(record)
            if 200 <= response_status < 300:
                status = "sent"
            else:
                last_error = f"http_status_{response_status}"
                if 400 <= response_status < 500 and response_status != 429:
                    status = "dead"
        except Exception as error:
            last_error = type(error).__name__
        attempts = int(record["attempts"])
        if status != "sent" and attempts >= MAX_DELIVERY_ATTEMPTS:
            status = "dead"
        updated = {
            **record, "timestamp": _iso(_utcnow()), "status": status,
            "next_attempt_at": _iso(_utcnow() + timedelta(seconds=self._backoff_seconds(attempts)))
                if status == "failed" else None,
            "last_error": last_error, "response_status": response_status,
        }
        saved = await self.store.finish(updated)
        logger.log(logging.INFO if saved and status == "sent" else logging.WARNING,
                   "delivery result delivery_id=%s status=%s saved=%s attempts=%s",
                   record["delivery_id"], status, saved, attempts)
        return updated if saved else {**updated, "status": "processing"}

    async def _send(self, record):
        if record["destination_type"] == TELEGRAM_LEAD_CARD:
            if self.telegram_bridge is None:
                return 503
            return await self.telegram_bridge.send_outbox_lead_card(
                company_id=record["company_id"], session_id=record["session_id"], card=record["payload"],
            )
        # Время события неизменно, время последней попытки хранится отдельно.
        return await super()._send({**record, "timestamp": record.get("event_timestamp") or record["timestamp"]})

    async def run_retry_loop(self, interval_seconds):
        while True:
            self._wakeup.clear()
            try:
                await self.retry_due()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("delivery retry failed error=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    def _latest_records(self):
        raise LeadStorageError("Для SQL-очереди требуется асинхронное чтение")

    async def get_summary(self, *, company_id=None, limit=50):
        try:
            return await self.store.reports.outbox_summary(company_id=company_id, limit=limit)
        except Exception as error:
            logger.error("delivery summary failed error=%s", type(error).__name__)
            raise LeadStorageError("Очередь недоступна") from None

    async def get_health(self):
        try:
            result = await self.store.reports.outbox_health()
            readiness = await self.store.reports.readiness()
            result["writable"] = readiness["writable"]
            if not readiness["writable"]:
                result["status"] = "error"
                result["detail"] = "БД недоступна для записи заявок и отправок"
            return result
        except Exception as error:
            logger.error("delivery health failed error=%s", type(error).__name__)
            return {"status": "error", "storage": "postgres", "detail": "Очередь БД недоступна"}


class PostgresLeadService:
    def __init__(self, *, store, delivery_service: PostgresDeliveryService):
        self.store = store
        self.delivery_service = delivery_service

    async def save(self, lead: Lead, event_type="lead_created", *, telegram_card=None):
        payload = lead_to_payload(lead)
        try:
            receipt = await prepare_lead_receipt(lead, event_type)
            if receipt is not None:
                operation, _ = receipt
                lead.storage_id = str(uuid5(NAMESPACE_URL, f"chat:{operation.company_id}:{operation.request_id}"))
            records = self.delivery_service.prepare_event(
                event_type=event_type, company_id=lead.company_id, session_id=lead.session_id, payload=payload)
            if telegram_card is not None:
                records.append({
                    "delivery_id": str(uuid5(NAMESPACE_URL, f"telegram-lead:{lead.storage_id}")),
                    "event_type": event_type, "company_id": lead.company_id, "session_id": lead.session_id,
                    "destination_type": TELEGRAM_LEAD_CARD, "payload": telegram_card,
                })
            kwargs = {"request_receipt": receipt} if receipt is not None else {}
            await self.store.save_lead(lead_id=lead.storage_id, event_type=event_type,
                                       payload=payload, records=records, **kwargs)
            mark_lead_committed(receipt)
        except Exception as error:
            logger.error("lead save failed company_id=%s error=%s", lead.company_id, type(error).__name__)
            raise LeadStorageError("Не удалось сохранить заявку") from None
        self.delivery_service._wakeup.set()
