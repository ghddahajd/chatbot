"""Повтор не запускает диалог заново; потерянный результат не считается отказом записи."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import AsyncMock
from uuid import uuid4

from app.models import ChatMessageResponse, PolicyAction, SessionStatus
from app.services.chat_service import ChatService
from app.utils.jsonl import read_jsonl


def contact(client):
    started = client.post("/api/chat/message", json={"company_id": "rosh_demo", "message": "Хочу записаться"})
    assert started.status_code == 200
    return {"request_id": str(uuid4()), "company_id": "rosh_demo", "session_id": started.json()["session_id"],
            "message": "Тест +7 999 123-45-67", "booking_day": "Завтра", "page": "/"}


def test_retry_returns_original_response_without_second_lead(test_client, managed_env):
    body = contact(test_client)
    first = test_client.post("/api/chat/message", json=body)
    assert first.status_code == 200 and first.json()["lead_created"]
    second = test_client.post("/api/chat/message", json={**body, "retry": True})
    assert second.status_code == 200 and second.json() == first.json()
    assert len(read_jsonl(managed_env["temp_dir"] / "leads.jsonl")) == 1
    result = test_client.post("/api/chat/message/result", json=body)
    assert result.json()["state"] == "done"
    assert result.json()["response"] == first.json()
    assert not result.json()["durable"]


def test_same_request_id_with_different_data_is_conflict(test_client, managed_env):
    body = contact(test_client)
    assert test_client.post("/api/chat/message", json=body).status_code == 200
    changed = {**body, "booking_day": "Послезавтра"}
    assert test_client.post("/api/chat/message", json=changed).status_code == 409
    assert test_client.post("/api/chat/message/result", json=changed).status_code == 409
    assert len(read_jsonl(managed_env["temp_dir"] / "leads.jsonl")) == 1


def test_memory_restart_does_not_blindly_resubmit_saved_lead(test_client, managed_env):
    from app.storage.request_receipts import MemoryRequestReceipts

    body = contact(test_client)
    assert test_client.post("/api/chat/message", json=body).json()["lead_created"]
    test_client.app.state.request_receipts = MemoryRequestReceipts()
    replay = test_client.post("/api/chat/message", json={**body, "retry": True})
    assert replay.status_code == 202
    assert replay.json()["state"] == "unknown" and not replay.json()["retry_allowed"]
    assert len(read_jsonl(managed_env["temp_dir"] / "leads.jsonl")) == 1


def test_interrupt_after_file_write_recovers_truthful_ack(test_client, managed_env, monkeypatch):
    body = contact(test_client)
    monkeypatch.setattr(test_client.app.state.session_store, "set_lead_requested",
                        AsyncMock(side_effect=RuntimeError("private diagnostic")))
    response = test_client.post("/api/chat/message", json=body)
    assert response.status_code == 200 and response.json()["lead_created"]
    assert "private diagnostic" not in response.text
    assert test_client.post("/api/chat/message", json={**body, "retry": True}).json() == response.json()
    assert len(read_jsonl(managed_env["temp_dir"] / "leads.jsonl")) == 1


def test_failed_write_is_not_acknowledged_or_blindly_repeated(test_client, managed_env, monkeypatch):
    body = contact(test_client)

    def fail(payload):
        raise OSError("private diagnostic")

    monkeypatch.setattr(test_client.app.state.lead_service.store, "append", fail)
    response = test_client.post("/api/chat/message", json=body)
    assert response.status_code == 503 and "private diagnostic" not in response.text
    replay = test_client.post("/api/chat/message", json={**body, "retry": True})
    assert replay.status_code == 202 and replay.json()["state"] == "uncertain"
    assert not read_jsonl(managed_env["temp_dir"] / "leads.jsonl")


def test_parallel_duplicate_does_not_start_second_pipeline(test_client, monkeypatch):
    import asyncio

    started = Event()
    released = asyncio.Event()
    calls = []

    async def handle(self, **kwargs):
        calls.append(kwargs)
        started.set()
        await released.wait()
        return ChatMessageResponse(session_id="one-session", status=SessionStatus.AI_ACTIVE,
                                   action=PolicyAction.ANSWER, answer="Готово")

    monkeypatch.setattr(ChatService, "handle_message", handle)
    body = {"company_id": "rosh_demo", "message": "Привет", "request_id": str(uuid4())}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(test_client.post, "/api/chat/message", json=body)
        try:
            assert started.wait(3)
            duplicate = test_client.post("/api/chat/message", json={**body, "retry": True})
            assert duplicate.status_code == 202 and duplicate.json()["state"] == "processing"
            assert len(calls) == 1
        finally:
            test_client.portal.call(released.set)
        assert pending.result(timeout=3).status_code == 200
    assert test_client.post("/api/chat/message", json=body).status_code == 200
    assert len(calls) == 1
