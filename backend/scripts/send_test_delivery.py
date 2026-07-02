"""отправляет тестовое delivery-событие для проверки каналов клиента."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
SESSION_ID = "test-session"
EVENTS = ("lead_created", "booking_created", "operator_requested")

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))


def _payload_for(event_type: str) -> dict[str, Any]:
    if event_type == "booking_created":
        return {
            "name": "Тест Тестов",
            "phone": "+7 000 000-00-00",
            "summary": "[TEST] хочет записаться",
            "service_id": None,
            "session_id": SESSION_ID,
            "source": "send_test_delivery",
        }
    if event_type == "operator_requested":
        return {
            "last_message": "[TEST] позовите оператора",
            "session_id": SESSION_ID,
            "source": "send_test_delivery",
        }
    return {
        "name": "Тест Тестов",
        "phone": "+7 000 000-00-00",
        "summary": "[TEST] интересуется услугами",
        "session_id": SESSION_ID,
        "source": "send_test_delivery",
    }


def _build_delivery_service():
    from app.config import get_settings  # noqa: WPS433
    from app.delivery import DeliveryService  # noqa: WPS433
    from app.knowledge import KnowledgeBaseResolver  # noqa: WPS433

    settings = get_settings()
    resolver = KnowledgeBaseResolver(
        data_dir=settings.data_dir,
        clients_data_dir=settings.clients_data_dir,
        defaults_data_dir=settings.defaults_data_dir,
        default_company_id=settings.default_company_id,
    )
    resolver.build_domain_index()
    return DeliveryService(
        outbox_file=settings.delivery_outbox_file,
        knowledge_base_resolver=resolver,
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )


def _print_header(company_id: str, event_type: str) -> None:
    print(f"Delivery Test — {company_id}")
    print(f"event: {event_type}")
    print("────────────────────────────")


def _print_dry_run(company_id: str, event_type: str, payload: dict[str, Any], destinations: list[dict[str, str]]) -> None:
    _print_header(company_id, event_type)
    print("destinations:")
    if not destinations:
        print("  ℹ️  all:      not configured")
    for destination in destinations:
        print(f"  ℹ️  {destination['type']:<9} would send (target: {destination.get('target')})")
    print("payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_result(records: list[dict[str, Any]], destinations: list[dict[str, str]]) -> None:
    print("destinations:")
    if records:
        print(f"  ✅ jsonl:    record created (delivery_id: {records[0].get('delivery_id')})")
    else:
        print("  ℹ️  jsonl:    no record created")

    configured_types = {destination["type"] for destination in destinations}
    result_by_type = {str(record.get("destination_type")): record for record in records}

    for destination_type in ("telegram", "webhook"):
        if destination_type not in configured_types:
            print(f"  ℹ️  {destination_type:<9} not configured")
            continue

        record = result_by_type.get(destination_type)
        if record is None:
            print(f"  ℹ️  {destination_type:<9} not configured")
            continue

        status = str(record.get("status") or "")
        response_status = record.get("response_status")
        target = record.get("target")
        if status == "sent":
            print(f"  ✅ {destination_type:<9} sent (status {response_status})")
        else:
            reason = record.get("last_error") or f"status={status}"
            print(f"  ⚠️  {destination_type:<9} failed — {reason} (url: {target})")


async def _run_event(company_id: str, event_type: str, *, dry_run: bool) -> None:
    delivery_service = _build_delivery_service()
    payload = _payload_for(event_type)
    destinations = delivery_service._destinations_for(company_id=company_id, event_type=event_type)

    if dry_run:
        _print_dry_run(company_id, event_type, payload, destinations)
        return

    _print_header(company_id, event_type)
    records = await delivery_service.enqueue_event(
        event_type=event_type,
        company_id=company_id,
        session_id=SESSION_ID,
        payload=payload,
    )
    _print_result(records, destinations)


async def _run(args: argparse.Namespace) -> None:
    events = EVENTS if args.event == "all" else (args.event,)
    for index, event_type in enumerate(events):
        if index:
            print("")
        await _run_event(args.company, event_type, dry_run=args.dry_run)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Отправить тестовое delivery-событие клиента.")
    parser.add_argument("--company", required=True, help="company_id клиента")
    parser.add_argument("--event", choices=(*EVENTS, "all"), default="lead_created")
    parser.add_argument("--dry-run", action="store_true", help="Показать destinations и payload без отправки")
    return parser.parse_args()


def main() -> int:
    try:
        asyncio.run(_run(parse_args()))
    except Exception as error:
        print(f"Delivery test failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
