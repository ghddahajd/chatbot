"""Receipts using the lead store's existing engine and session factory."""

from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from ..db.request_models import chat_requests as requests
from .request_receipts import LEASE_SECONDS, RequestConflict, identity, response_copy


class PostgresRequestReceipts:
    durable = True

    def __init__(self, lead_store):
        self.lead_store = lead_store

    @staticmethod
    def _view(row, fingerprint):
        if row is None:
            return {"state": "unknown", "durable": True, "retry_allowed": True}
        if row["fingerprint"] != fingerprint:
            raise RequestConflict("Request ID belongs to another request")
        if row["response"] is not None:
            return {"state": "done", "response": row["response"], "durable": True}
        active = row["status"] == "processing" and row["lease_active"]
        return {"state": "processing" if active else "uncertain", "durable": True}

    async def _read(self, session, tenant_id, request_id):
        return (await session.execute(sa.select(
            requests, (requests.c.lease_until > sa.func.clock_timestamp()).label("lease_active")
        ).where(requests.c.tenant_id == tenant_id, requests.c.request_id == UUID(request_id)))).mappings().one_or_none()

    @staticmethod
    async def _lock(session, tenant_id, request_id):
        # Evaluate lease fencing in the NEXT statement, after any row-lock wait.
        await session.execute(sa.select(requests.c.request_id).where(
            requests.c.tenant_id == tenant_id, requests.c.request_id == request_id,
        ).with_for_update())

    async def begin(self, company_id, request_id: str, fingerprint: str, retry=False) -> dict:
        company_id, request_id = identity(company_id, request_id, fingerprint)
        async with self.lead_store.sessions() as session, session.begin():
            tenant_id = await self.lead_store._tenant(session, company_id)
            token = uuid4()
            now = sa.func.clock_timestamp()
            # The composite PK arbitrates first submission AND a same-ID retry after restart.
            inserted = await session.scalar(insert(requests).values(
                tenant_id=tenant_id, request_id=UUID(request_id), fingerprint=fingerprint,
                claim_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS),
            ).on_conflict_do_nothing(index_elements=[requests.c.tenant_id, requests.c.request_id])
                .returning(requests.c.request_id))
            if inserted is not None:
                return {"state": "started", "claim_token": str(token), "durable": True}
            return self._view(await self._read(session, tenant_id, request_id), fingerprint)

    async def status(self, company_id, request_id: str, fingerprint: str) -> dict:
        company_id, request_id = identity(company_id, request_id, fingerprint)
        async with self.lead_store.sessions() as session:
            tenant_id = await self.lead_store._tenant(session, company_id)
            return self._view(await self._read(session, tenant_id, request_id), fingerprint)

    async def finish(self, company_id, request_id, claim_token, result: dict) -> bool:
        result = response_copy(result)
        try:
            token, rid = UUID(str(claim_token)), UUID(request_id)
        except (ValueError, AttributeError, TypeError):
            return False
        async with self.lead_store.sessions() as session, session.begin():
            tenant_id = await self.lead_store._tenant(session, company_id)
            await self._lock(session, tenant_id, rid)
            updated = await session.scalar(requests.update().where(
                requests.c.tenant_id == tenant_id, requests.c.request_id == rid,
                requests.c.claim_token == token, requests.c.status == "processing",
                sa.or_(requests.c.lease_until > sa.func.clock_timestamp(),
                       sa.and_(requests.c.lead_id.is_not(None), requests.c.response.is_not(None))),
            ).values(status="done", response=result, updated_at=sa.func.clock_timestamp())
                .returning(requests.c.request_id))
            return updated is not None

    async def fail(self, company_id, request_id, claim_token) -> None:
        try:
            token, rid = UUID(str(claim_token)), UUID(request_id)
        except (ValueError, AttributeError, TypeError):
            return
        async with self.lead_store.sessions() as session, session.begin():
            tenant_id = await self.lead_store._tenant(session, company_id)
            # A failed callback must not erase a lead committed earlier in that callback.
            await session.execute(requests.update().where(
                requests.c.tenant_id == tenant_id, requests.c.request_id == rid,
                requests.c.claim_token == token, requests.c.status == "processing",
            ).values(status="uncertain", updated_at=sa.func.clock_timestamp()))

    async def checkpoint_lead(self, session, context, lead_id, result) -> None:
        if context.receipts is not self:
            raise ValueError("Request receipt store mismatch")
        if session.bind is not self.lead_store.engine:
            raise ValueError("Request receipt transaction mismatch")
        company_id, request_id = identity(context.company_id, context.request_id, context.fingerprint)
        result = response_copy(result)
        tenant_id = await self.lead_store._tenant(session, company_id)
        await self._lock(session, tenant_id, UUID(request_id))
        updated = await session.scalar(requests.update().where(
            requests.c.tenant_id == tenant_id, requests.c.request_id == UUID(request_id),
            requests.c.fingerprint == context.fingerprint,
            requests.c.claim_token == UUID(str(context.claim_token)), requests.c.status == "processing",
            requests.c.lease_until > sa.func.clock_timestamp(),
            sa.or_(requests.c.lead_id.is_(None), requests.c.lead_id == lead_id),
        ).values(response=result, lead_id=lead_id, updated_at=sa.func.clock_timestamp())
            .returning(requests.c.request_id))
        if updated is None:
            raise ValueError("Request claim is no longer active")
