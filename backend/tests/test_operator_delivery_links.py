"""Ссылки не содержат секретов, отказ очереди не подтверждается без работающего моста."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.chat_service import ChatService
from app.storage.errors import LeadStorageError


def test_operator_links_require_auth_without_exposing_token():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(operator_token="must-not-be-in-payload"))),
        url_for=lambda name: "https://example.invalid/operator")
    service = ChatService(request)
    assert service._operator_url() == "https://example.invalid/operator"
    assert service._operator_session_url("test-session") == "https://example.invalid/operator?session_id=test-session"


def test_generic_failure_without_bridge_does_not_ack_operator():
    state = SimpleNamespace(delivery_service=SimpleNamespace(enqueue_event=AsyncMock(side_effect=LeadStorageError())),
                            telegram_bridge_service=None)
    service = ChatService(SimpleNamespace(app=SimpleNamespace(state=state), url_for=lambda name: "/operator"))
    assert asyncio.run(service._enqueue_operator_requested(company_id="rosh_demo", session_id="test", message="Test")) is False
