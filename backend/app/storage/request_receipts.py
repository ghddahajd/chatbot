"""Process-local receipts; deliberately independent of SQL and runtime imports."""

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from time import monotonic
from uuid import UUID, uuid4


LEASE_SECONDS = 300


class RequestConflict(ValueError):
    """The request ID already belongs to another fingerprint."""


def identity(company_id, request_id, fingerprint):
    if not isinstance(company_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", company_id):
        raise ValueError("Invalid company ID")
    if not isinstance(request_id, str):
        raise ValueError("Invalid request ID")
    try:
        request_id = str(UUID(request_id))
    except (ValueError, AttributeError):
        raise ValueError("Invalid request ID") from None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid request fingerprint")
    return company_id, request_id


def response_copy(result):
    if not isinstance(result, dict):
        raise ValueError("Request response must be an object")
    try:
        return json.loads(json.dumps(result, allow_nan=False))
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Request response must be JSON") from None


@dataclass
class _Receipt:
    fingerprint: str
    claim_token: str
    lease_until: float
    updated_at: float
    status: str = "processing"
    response: dict | None = None
    lead_id: str | None = None
    checkpoint_ready: bool = False


class MemoryRequestReceipts:
    durable = False

    def __init__(self, *, max_entries=2048, ttl_seconds=3600):
        if not isinstance(max_entries, int) or max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("Positive receipt capacity and TTL required")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._rows = {}

    def _prune(self):
        now = monotonic()
        for key, row in list(self._rows.items()):
            active = row.status == "processing" and row.lease_until > now
            if not active and now - row.updated_at >= self.ttl_seconds:
                del self._rows[key]

    def _view(self, row, fingerprint):
        if row is None:
            return {"state": "unknown", "durable": False, "retry_allowed": False}
        if row.fingerprint != fingerprint:
            raise RequestConflict("Request ID belongs to another request")
        if row.response is not None:
            return {"state": "done", "response": deepcopy(row.response), "durable": False}
        active = row.status == "processing" and row.lease_until > monotonic()
        return {"state": "processing" if active else "uncertain", "durable": False}

    async def begin(self, company_id, request_id: str, fingerprint: str, retry=False) -> dict:
        key = identity(company_id, request_id, fingerprint)
        self._prune()
        row = self._rows.get(key)
        if row is not None or retry:
            return self._view(row, fingerprint)
        if len(self._rows) >= self.max_entries:
            # A live claim must survive capacity pressure, including its checkpoint.
            candidates = [(k, r) for k, r in self._rows.items()
                          if r.status != "processing" or r.lease_until <= monotonic()]
            if not candidates:
                raise RuntimeError("Request receipt capacity reached")
            del self._rows[min(candidates, key=lambda item: item[1].updated_at)[0]]
        now = monotonic()
        row = _Receipt(fingerprint, str(uuid4()), now + LEASE_SECONDS, now)
        self._rows[key] = row
        return {"state": "started", "claim_token": row.claim_token, "durable": False}

    async def status(self, company_id, request_id: str, fingerprint: str) -> dict:
        key = identity(company_id, request_id, fingerprint)
        self._prune()
        return self._view(self._rows.get(key), fingerprint)

    async def finish(self, company_id, request_id, claim_token, result: dict) -> bool:
        try:
            key = (company_id, str(UUID(request_id)))
        except (ValueError, TypeError, AttributeError):
            return False
        row = self._rows.get(key)
        if row is None or row.claim_token != claim_token or row.status != "processing":
            return False
        if row.lease_until <= monotonic() and row.lead_id is None:
            return False
        row.response = response_copy(result)
        row.status = "done"
        row.updated_at = monotonic()
        return True

    async def fail(self, company_id, request_id, claim_token) -> None:
        try:
            key = (company_id, str(UUID(request_id)))
        except (ValueError, TypeError, AttributeError):
            return
        row = self._rows.get(key)
        if row is not None and row.claim_token == claim_token and row.status == "processing":
            row.status = "uncertain"
            row.updated_at = monotonic()

    def validate_claim(self, context) -> None:
        if context.receipts is not self:
            raise ValueError("Request receipt store mismatch")
        key = identity(context.company_id, context.request_id, context.fingerprint)
        row = self._rows.get(key)
        if (row is None or row.fingerprint != context.fingerprint
                or row.claim_token != context.claim_token or row.status != "processing"
                or row.lease_until <= monotonic()):
            raise ValueError("Request claim is no longer active")

    def checkpoint_lead(self, context, lead_id, result) -> None:
        if context.receipts is not self:
            raise ValueError("Request receipt store mismatch")
        key = identity(context.company_id, context.request_id, context.fingerprint)
        row = self._rows.get(key)
        if not (row is not None and row.fingerprint == context.fingerprint
                and row.claim_token == context.claim_token and row.status == "processing"):
            raise ValueError("Request claim is no longer active")
        if not row.checkpoint_ready:
            self.validate_claim(context)
        if not isinstance(lead_id, str) or not lead_id:
            raise ValueError("Invalid lead ID")
        row = self._rows[identity(context.company_id, context.request_id, context.fingerprint)]
        if row.lead_id is not None and row.lead_id != lead_id:
            raise ValueError("Request already references another lead")
        row.response = response_copy(result)
        row.lead_id = lead_id
        row.updated_at = monotonic()
        row.checkpoint_ready = False

    def checkpoint_ready(self, context) -> None:
        """Call immediately before sync append; no await before checkpoint_lead."""
        self.validate_claim(context)
        key = identity(context.company_id, context.request_id, context.fingerprint)
        self._rows[key].checkpoint_ready = True
