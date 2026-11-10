"""Совместимость файловых хранилищ с сервисами и данными после перезапуска."""

import asyncio
import json
from datetime import datetime

import pytest

from app.analytics import AnalyticsService
from app.delivery import DeliveryService
from app.leads import LeadService, lead_to_payload
from app.models import Lead
from app.storage.leads import FileLeadStore
from app.storage.outbox import FileOutboxStore
from app.utils.jsonl import append_jsonl, read_jsonl


def test_saved_lead_survives_reopen_and_is_visible_with_archive(tmp_path):
    path = tmp_path / "nested" / "leads.jsonl"
    archive = tmp_path / "archive.jsonl"
    lead = Lead(
        timestamp=datetime(2026, 1, 2, 12, 30),
        company_id="clinic_alpha", session_id="session-1", name="Тест",
        phone="+70000000000", summary="Перезвонить", service_id="consultation",
        reason="booking", needs_operator=True, lead_trigger="booking_request",
        unresolved_query="Вопрос", recent_messages=[{"role": "user", "text": "Запишите"}],
        preferred_time="Завтра", page="/consultation/",
    )
    asyncio.run(LeadService(path).save(lead, event_type="booking_created"))
    expected = lead_to_payload(lead)
    assert path.read_text(encoding="utf-8") == json.dumps(expected, ensure_ascii=False) + "\n"
    append_jsonl(archive, {"company_id": "clinic_beta", "session_id": "old"})

    reopened = FileLeadStore(path, archive)
    analytics = AnalyticsService(tmp_path / "analytics.jsonl", path, leads_archive_file=archive)
    assert reopened.read_all() == [expected, {"company_id": "clinic_beta", "session_id": "old"}]
    assert analytics._all_leads() == reopened.read_all()


def test_failed_lead_write_does_not_enqueue_notification(tmp_path, monkeypatch):
    class DeliveryRecorder:
        calls = 0

        async def enqueue_event(self, **kwargs):
            self.calls += 1

    delivery = DeliveryRecorder()
    service = LeadService(tmp_path / "leads.jsonl", delivery_service=delivery)

    def fail_write(payload):
        raise OSError("storage unavailable")

    monkeypatch.setattr(service.store, "append", fail_write)
    lead = Lead(company_id="clinic_alpha", session_id="session-1", name="Тест", phone="", summary="Тест")
    with pytest.raises(OSError, match="storage unavailable"):
        asyncio.run(service.save(lead))
    assert delivery.calls == 0


def test_outbox_reopen_keeps_latest_state_and_tolerates_legacy_lines(tmp_path):
    path = tmp_path / "outbox.jsonl"
    path.write_text('broken line\n[]\n{"status":"legacy"}\n', encoding="utf-8")

    async def write():
        store = FileOutboxStore(path)
        await store.append({"delivery_id": "alpha", "company_id": "clinic_alpha", "status": "pending"})
        await store.append({"delivery_id": "beta", "company_id": "clinic_beta", "status": "failed"})
        await store.append({"delivery_id": "alpha", "company_id": "clinic_alpha", "status": "sent"})

    asyncio.run(write())
    latest = FileOutboxStore(path).latest()
    assert list(latest) == ["alpha", "beta"]
    assert latest["alpha"]["status"] == "sent"
    assert latest["beta"]["status"] == "failed"
    assert len(read_jsonl(path)) == 4


def test_delivery_is_persisted_before_send_and_summary_survives_restart(tmp_path, resolver, monkeypatch):
    path = tmp_path / "outbox.jsonl"
    service = DeliveryService(outbox_file=path, knowledge_base_resolver=resolver)
    monkeypatch.setattr(service, "_destinations_for", lambda **kwargs: [{"type": "webhook", "target": "https://example.invalid"}])

    async def fake_send(record):
        saved = FileOutboxStore(path).latest()[record["delivery_id"]]
        assert saved == record
        assert saved["status"] == "pending"
        return 200

    monkeypatch.setattr(service, "_send", fake_send)
    records = asyncio.run(service.enqueue_event(
        event_type="operator_requested", company_id="rosh_demo", session_id="session-1",
        payload={"last_message": "Оператор"},
    ))
    reopened = DeliveryService(outbox_file=path, knowledge_base_resolver=resolver)
    assert reopened.summary() == service.summary()
    assert reopened.outbox_health() == service.outbox_health()
    assert reopened._latest_records()[records[0]["delivery_id"]] == records[0]
    assert records[0]["status"] == "sent"
