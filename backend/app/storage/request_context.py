"""Контекст одной отправки; подтверждение лида готовится до сохранения."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RequestOperation:
    receipts: Any
    company_id: str
    request_id: str
    fingerprint: str
    claim_token: str
    recovery_factory: Callable
    committed_response: dict | None = None


CURRENT_REQUEST: ContextVar[RequestOperation | None] = ContextVar("chat_request_operation", default=None)


async def prepare_lead_receipt(lead, event_type):
    operation = CURRENT_REQUEST.get()
    if operation is None:
        return None
    if lead.company_id != operation.company_id:
        raise ValueError("Request and lead company differ")
    if operation.committed_response is not None:
        raise ValueError("One request cannot create a second lead")
    return operation, await operation.recovery_factory(lead, event_type)


def mark_lead_committed(prepared):
    if prepared is not None:
        operation, result = prepared
        operation.committed_response = result
