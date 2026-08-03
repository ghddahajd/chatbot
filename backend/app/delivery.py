"""outbox-доставка лидов клиентским каналам."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx

from .knowledge import KnowledgeBaseResolver
from .leads import REASON_LABELS, lead_to_payload
from .models import Lead
from .utils.jsonl import read_jsonl


logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60
DELIVERY_TIMEOUT_SECONDS = 5.0


def _last_user_message_text(payload: dict[str, Any]) -> str:
    """последнее сообщение юзера, кроме того что просто повторяет уже показанный телефон.

    Иначе почти всегда это "Имя +телефон" — то же самое, что уже стоит в 👤/📞 выше,
    и строка "Последнее сообщение" дублирует контакты вместо того, чтобы нести новый факт.
    """

    recent_messages = payload.get("recent_messages")
    if not isinstance(recent_messages, list):
        return ""
    phone_digits = re.sub(r"\D", "", str(payload.get("phone") or ""))
    for item in reversed(recent_messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if phone_digits and phone_digits in re.sub(r"\D", "", text):
            continue
        return text
    return ""


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


_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def _escape_markdown(value: str) -> str:
    """экранирует спецсимволы Telegram Markdown (parse_mode=Markdown).

    Без этого голое "_" в любом динамическом значении (например `session_id=...`
    в operator_url, или подчёркивание внутри имени/саммари) ломает парсинг целиком:
    Telegram возвращает 400 "can't parse entities", и лид/уведомление не доставляется
    вообще — не просто криво отображается, а полностью проваливается.
    """

    for char in _MARKDOWN_SPECIAL_CHARS:
        value = value.replace(char, "\\" + char)
    return value


def _text_value(value: Any) -> str:
    text = str(value or "").strip()
    return _escape_markdown(text) if text else "не указано"


def _telegram_text(
    *,
    event_type: str,
    company_name: str,
    timestamp: str,
    payload: dict[str, Any],
    service_name: str = "",
) -> str:
    if event_type == "booking_created":
        service_line = f"\n✂️ {_text_value(service_name)}" if service_name else ""
        operator_url = _escape_markdown(str(payload.get("operator_url") or "").strip())
        dialog_line = f"\n\n🔗 Открыть диалог: {operator_url}" if operator_url else ""
        return (
            f"📅 *Запись на услугу* — {_text_value(company_name)}\n\n"
            f"👤 {_text_value(payload.get('name'))}\n"
            f"📞 {_text_value(payload.get('phone'))}"
            f"{service_line}\n"
            f"💬 {_text_value(payload.get('summary'))}\n"
            f"🕐 {_text_value(timestamp)}"
            f"{dialog_line}"
        )

    if event_type == "operator_requested":
        operator_url = _escape_markdown(str(payload.get("operator_url") or "").strip())
        operator_line = f"\n🔗 Открыть панель: {operator_url}" if operator_url else ""
        return (
            f"⚡️ *Клиент просит оператора* — {_text_value(company_name)}\n\n"
            f"💬 \"{_text_value(payload.get('last_message'))}\"\n"
            f"🕐 {_text_value(timestamp)}"
            f"{operator_line}"
        )

    reason_line = REASON_LABELS.get(str(payload.get("reason") or ""), "Оператор/контакт")
    last_message = _last_user_message_text(payload)
    operator_url = _escape_markdown(str(payload.get("operator_url") or "").strip())
    dialog_line = f"\n\n🔗 Открыть диалог: {operator_url}" if operator_url else ""
    # needs_operator помечает лиды, где юзер уже упирался в мед-риск/регулируемую тему —
    # им нужен более быстрый ответ, чем обычному лиду с ценой; выделяем визуально, а не
    # только текстом в саммари, чтобы это было видно не читая карточку целиком.
    title = "🔴 *Срочная заявка*" if payload.get("needs_operator") else "🔔 *Новая заявка*"

    if str(payload.get("lead_trigger") or "") == "unknown_service":
        unresolved_query = str(payload.get("unresolved_query") or "").strip()
        # "Последнее сообщение" тут почти всегда дублирует 👤/📞 (сообщение с контактом) —
        # "Запрос" уже несёт содержательную часть, отдельный хвост не нужен.
        return (
            f"{title} — {_text_value(company_name)}\n\n"
            f"👤 Имя: {_text_value(payload.get('name'))}\n"
            f"📞 Телефон: {_text_value(payload.get('phone'))}\n\n"
            f"🏷 Тип: {_text_value(REASON_LABELS['unknown_service'])}\n"
            f"❓ Запрос: {_text_value(unresolved_query)}\n"
            f"✂️ Услуга в базе: не найдена\n\n"
            f"🕐 {_text_value(timestamp)}"
            f"{dialog_line}"
        )

    service_line = f"✂️ Услуга: {_text_value(service_name)}\n" if service_name else ""
    last_message_line = f"🗨 Последнее сообщение: {_text_value(last_message)}\n" if last_message else ""
    return (
        f"{title} — {_text_value(company_name)}\n\n"
        f"👤 Имя: {_text_value(payload.get('name'))}\n"
        f"📞 Телефон: {_text_value(payload.get('phone'))}\n"
        f"{service_line}"
        f"🏷 Тип запроса: {reason_line}\n"
        f"💬 Саммари: {_text_value(payload.get('summary'))}\n"
        f"{last_message_line}"
        f"🕐 {_text_value(timestamp)}"
        f"{dialog_line}"
    )


def _webhook_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": record.get("event_type") or "lead_created",
        "delivery_id": record.get("delivery_id"),
        "company_id": record.get("company_id"),
        "timestamp": record.get("timestamp"),
        "data": record.get("payload") or {},
    }


def _event_enabled(config: dict[str, Any], event_type: str) -> bool:
    events = config.get("events")
    if not isinstance(events, list):
        return False
    return event_type in {str(event) for event in events}


class DeliveryService:
    """пишет outbox и доставляет лиды в Telegram/webhook с retry metadata."""

    def __init__(
        self,
        *,
        outbox_file: Path,
        knowledge_base_resolver: KnowledgeBaseResolver,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        telegram_dm_enabled: bool = False,
    ) -> None:
        self.outbox_file = outbox_file
        self.knowledge_base_resolver = knowledge_base_resolver
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.telegram_dm_enabled = telegram_dm_enabled
        self._lock = asyncio.Lock()

    async def enqueue_lead(self, lead: Lead) -> list[dict[str, Any]]:
        """ставит событие создания лида в generic delivery outbox."""

        return await self.enqueue_event(
            event_type="lead_created",
            company_id=lead.company_id,
            session_id=lead.session_id,
            payload=lead_to_payload(lead),
        )

    async def enqueue_event(
        self,
        *,
        event_type: str,
        company_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """создаёт outbox-записи события и сразу делает первую попытку доставки."""

        records = []
        for destination in self._destinations_for(company_id=company_id, event_type=event_type):
            record = {
                "timestamp": _iso(_utcnow()),
                "delivery_id": str(uuid4()),
                "event_type": event_type,
                "company_id": company_id,
                "session_id": session_id,
                "destination_type": destination["type"],
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": _iso(_utcnow()),
                "target": destination.get("target"),
                "payload": payload,
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

    async def run_retry_loop(self, interval_seconds: int) -> None:
        """периодически повторяет due-доставки, пока задачу не отменят."""

        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self.retry_due()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("delivery retry loop error=%s", type(error).__name__)

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

    def _destinations_for(self, *, company_id: str, event_type: str) -> list[dict[str, str]]:
        destinations: list[dict[str, str]] = []
        try:
            knowledge_base = self.knowledge_base_resolver.get(company_id, fallback=False)
        except KeyError:
            logger.warning("delivery skipped destinations for unknown company_id=%s", company_id)
            return destinations

        notifications = self.knowledge_base_resolver.notifications_config(company_id)
        if notifications:
            telegram_config = notifications.get("telegram")
            if isinstance(telegram_config, dict):
                chat_id = str(telegram_config.get("chat_id") or "").strip()
                if (
                    bool(telegram_config.get("enabled"))
                    and self.telegram_bot_token
                    and chat_id
                    and _event_enabled(telegram_config, event_type)
                ):
                    destinations.append({"type": "telegram", "target": chat_id})

            webhook_config = notifications.get("webhook")
            if isinstance(webhook_config, dict):
                webhook_url = str(webhook_config.get("url") or "").strip()
                if (
                    bool(webhook_config.get("enabled"))
                    and webhook_url
                    and _event_enabled(webhook_config, event_type)
                ):
                    destinations.append({"type": "webhook", "target": webhook_url})

            return destinations

        # Раньше этот фолбэк срабатывал всегда, дублируя каждый лид/эскалацию личным
        # sendMessage в обход Тем группы операторов — теперь по умолчанию выключен
        # (telegram_dm_enabled), Темы/General/Клиенты (telegram_bridge.py) — основной путь.
        if self.telegram_dm_enabled and self.telegram_bot_token and self.telegram_chat_id:
            destinations.append({"type": "telegram", "target": self.telegram_chat_id})

        if knowledge_base.company.lead_webhook_url:
            destinations.append({"type": "webhook", "target": knowledge_base.company.lead_webhook_url})

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
        event_type = str(record.get("event_type") or "lead_created")
        company_id = str(record.get("company_id") or "")
        timestamp = str(record.get("timestamp") or "")
        company_name = self._company_name(company_id)
        service_name = self._service_name(company_id, payload.get("service_id"))

        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            if destination_type == "telegram":
                url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                chat_id = str(record.get("target") or self.telegram_chat_id)
                request_body: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": _telegram_text(
                        event_type=event_type,
                        company_name=company_name,
                        timestamp=timestamp,
                        payload=payload,
                        service_name=service_name,
                    ),
                    "parse_mode": "Markdown",
                }
                operator_url = str(payload.get("operator_url") or "").strip()
                # Telegram отклоняет ВСЁ сообщение (400 "Wrong HTTP URL"), если url кнопки не
                # https/tg — а operator_url на локальном/демо-стенде это http://localhost:...
                # Без этой проверки доставка полностью падает, а не просто "кнопки нет".
                if operator_url.startswith("https://") or operator_url.startswith("tg://"):
                    request_body["reply_markup"] = {
                        "inline_keyboard": [[{"text": "Открыть диалог", "url": operator_url}]]
                    }
                response = await client.post(url, json=request_body)
                return response.status_code

            if destination_type == "webhook":
                target = str(record.get("target") or "")
                response = await client.post(
                    target,
                    json=_webhook_payload(record),
                    headers={
                        "X-Delivery-ID": delivery_id,
                        "X-Widget-Event": event_type,
                        "X-Company-ID": str(record.get("company_id") or ""),
                    },
                )
                return response.status_code

        raise httpx.HTTPError(f"unsupported destination_type={destination_type}")

    def _company_name(self, company_id: str) -> str:
        try:
            return self.knowledge_base_resolver.get(company_id, fallback=False).company.company_name
        except KeyError:
            return company_id

    def _service_name(self, company_id: str, service_id: Any) -> str:
        if not service_id:
            return ""
        try:
            service = self.knowledge_base_resolver.get(company_id, fallback=False).find_service_by_id(str(service_id))
        except KeyError:
            return str(service_id)
        return service.name if service is not None else str(service_id)

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
