"""Границы commit/отправки и отказов; транзакции проверяются отдельно на PostgreSQL."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import Lead
from app.services.postgres_delivery import PostgresDeliveryService, PostgresLeadService
from app.storage.errors import LeadStorageError


def _delivery(resolver, store=None):
    return PostgresDeliveryService(store=store or SimpleNamespace(), knowledge_base_resolver=resolver)


def _claimed(attempts=1):
    return {"delivery_id": "delivery-test", "attempts": attempts, "status": "processing",
            "timestamp": "2026-01-01T10:00:00", "claim_token": "claim-test"}


def test_lead_commit_enqueues_all_destinations_before_worker(resolver, monkeypatch):
    store = SimpleNamespace(save_lead=AsyncMock())
    delivery = _delivery(resolver, store)
    monkeypatch.setattr(delivery, "_destinations_for", lambda **kwargs: [
        {"type": "webhook", "target": "https://example.invalid/one"},
        {"type": "webhook", "target": "https://example.invalid/two"},
    ])
    monkeypatch.setattr(delivery, "_send", AsyncMock())
    lead = Lead(company_id="rosh_demo", session_id="test-session", name="Test", phone="", summary="Test")
    asyncio.run(PostgresLeadService(store=store, delivery_service=delivery).save(lead))
    kwargs = store.save_lead.await_args.kwargs
    assert len(kwargs["records"]) == 2
    assert kwargs["lead_id"] == lead.storage_id
    assert kwargs["payload"]["company_id"] == "rosh_demo"
    assert "storage_id" not in kwargs["payload"] and "storage_id" not in lead.model_dump()
    delivery._send.assert_not_awaited()
    assert delivery._wakeup.is_set()


def test_lead_failure_propagates_without_success_or_sending(resolver):
    store = SimpleNamespace(save_lead=AsyncMock(side_effect=RuntimeError("private SQL values")))
    delivery = _delivery(resolver, store)
    lead = Lead(company_id="rosh_demo", session_id="test-session", name="Test", phone="", summary="Test")
    with pytest.raises(LeadStorageError) as failure:
        asyncio.run(PostgresLeadService(store=store, delivery_service=delivery).save(lead))
    assert "private SQL values" not in str(failure.value)
    assert not delivery._wakeup.is_set()


def test_unknown_company_is_not_a_valid_empty_destination_list(resolver):
    store = SimpleNamespace(enqueue=AsyncMock())
    delivery = _delivery(resolver, store)
    with pytest.raises(LeadStorageError):
        asyncio.run(delivery.enqueue_event(event_type="operator_requested", company_id="missing-clinic",
                                           session_id="test-session", payload={}))
    store.enqueue.assert_not_awaited()


@pytest.mark.parametrize("http_status,attempts,status", [(200, 1, "sent"), (400, 1, "dead"),
                          (429, 1, "failed"), (503, 1, "failed"), (503, 5, "dead")])
def test_statuses_and_fenced_completion(resolver, monkeypatch, http_status, attempts, status):
    store = SimpleNamespace(finish=AsyncMock(return_value=True))
    delivery = _delivery(resolver, store)
    monkeypatch.setattr(delivery, "_send", AsyncMock(return_value=http_status))
    result = asyncio.run(delivery._dispatch_claimed(_claimed(attempts)))
    assert result["status"] == status
    assert result["attempts"] == attempts
    assert (result["next_attempt_at"] is not None) == (status == "failed")
    assert store.finish.await_args.args[0]["claim_token"] == "claim-test"


def test_unexpected_send_error_does_not_block_next_delivery(resolver, monkeypatch):
    store = SimpleNamespace(claim=AsyncMock(side_effect=[[_claimed()], [_claimed()], []]),
                            finish=AsyncMock(return_value=True))
    delivery = _delivery(resolver, store)
    monkeypatch.setattr(delivery, "_send", AsyncMock(side_effect=[RuntimeError("not logged"), 200]))
    result = asyncio.run(delivery.retry_due())
    assert result == {"attempted": 2, "sent": 1, "failed": 1, "dead": 0}
    assert store.finish.await_args_list[0].args[0]["last_error"] == "RuntimeError"
    assert all(call.kwargs["limit"] == 1 for call in store.claim.await_args_list)


def test_cancelled_send_leaves_claim_for_lease_recovery(resolver, monkeypatch):
    store = SimpleNamespace(finish=AsyncMock())
    delivery = _delivery(resolver, store)
    monkeypatch.setattr(delivery, "_send", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(delivery._dispatch_claimed(_claimed()))
    store.finish.assert_not_awaited()


def test_stale_completion_is_not_reported_as_sent(resolver, monkeypatch):
    delivery = _delivery(resolver, SimpleNamespace(finish=AsyncMock(return_value=False)))
    monkeypatch.setattr(delivery, "_send", AsyncMock(return_value=200))
    assert asyncio.run(delivery._dispatch_claimed(_claimed()))["status"] == "processing"


def test_queue_health_reports_stuck_work_without_leaking_payload(resolver):
    store = SimpleNamespace(outbox_records=AsyncMock(return_value=[{
        **_claimed(), "company_id": "rosh_demo", "lease_until": "2026-01-01T10:00:00+00:00",
        "payload": {"phone": "not-for-health"},
    }]))
    result = asyncio.run(_delivery(resolver, store).get_health())
    assert result["status"] == "degraded" and result["oldest_due_seconds"] >= 300
    assert result["pending_events"] == 1 and "not-for-health" not in str(result)
