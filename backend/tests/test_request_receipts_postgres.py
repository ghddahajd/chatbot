"""Opt-in native unittest: migrated isolated PG, no HTTP or external sends.

DB_INTEGRATION_TEST=1 TEST_OWNER_DATABASE_URL and TEST_LEAD_DATABASE_URL (or LEAD_PG*)
must point to the parent's disposable integration database with runtime grants.
PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_request_receipts_postgres.py -v
"""

import asyncio
import importlib.util
import os
from hashlib import sha256
from types import SimpleNamespace
import unittest
from uuid import uuid4

if not importlib.util.find_spec("sqlmodel") or os.environ.get("DB_INTEGRATION_TEST") != "1":
    raise unittest.SkipTest("Requires isolated PostgreSQL with DB_INTEGRATION_TEST=1")

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from app.db.connection import make_engine
from app.db.models import Tenant
from app.db.lead_models import leads, delivery_outbox as outbox
from app.db.request_models import chat_requests as requests
from app.storage.postgres import PostgresLeadOutboxStore
from app.storage.postgres_receipts import PostgresRequestReceipts
from app.storage.request_receipts import RequestConflict


HASH = sha256(b"request").hexdigest()
OTHER = sha256(b"other request").hexdigest()


class RequestReceiptsPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = create_async_engine(os.environ["TEST_OWNER_DATABASE_URL"], hide_parameters=True)
        url = os.environ.get("TEST_LEAD_DATABASE_URL")
        engine = create_async_engine(url, hide_parameters=True) if url else make_engine("LEAD_")
        self.store = PostgresLeadOutboxStore(engine)
        self.receipts = PostgresRequestReceipts(self.store)
        self.addAsyncCleanup(self.owner.dispose)
        self.addAsyncCleanup(self.store.close)
        self.cids = ["request_test_" + uuid4().hex for _ in range(2)]
        self.tids = [uuid4(), uuid4()]
        self.rid = str(uuid4())
        async with self.owner.begin() as conn:
            for cid, tid in zip(self.cids, self.tids):
                await conn.execute(sa.insert(Tenant).values(id=tid, company_id=cid, company_name="Test"))
        self.addAsyncCleanup(self.remove_tenants)
        await self.store.initialize()

    async def remove_tenants(self):
        async with self.owner.begin() as conn:
            for table in (requests, outbox, leads):
                await conn.execute(table.delete().where(table.c.tenant_id.in_(self.tids)))
            await conn.execute(sa.delete(Tenant).where(Tenant.id.in_(self.tids)))

    async def begin(self, **kwargs):
        return await self.receipts.begin(self.cids[0], self.rid, HASH, **kwargs)

    async def status(self):
        return await self.receipts.status(self.cids[0], self.rid, HASH)

    async def context(self):
        claim = await self.begin()
        return SimpleNamespace(receipts=self.receipts, company_id=self.cids[0], request_id=self.rid,
                               fingerprint=HASH, claim_token=claim["claim_token"])

    async def expire(self):
        async with self.owner.begin() as conn:
            await conn.execute(requests.update().where(
                requests.c.tenant_id == self.tids[0], requests.c.request_id == self.rid,
            ).values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'")))

    async def save(self, ctx, response=None, **changes):
        lead_id = changes.pop("lead_id", str(uuid4()))
        await self.store.save_lead(
            lead_id=lead_id, event_type="lead_created",
            payload={"company_id": self.cids[0], "session_id": "session"},
            records=changes.pop("records", []),
            request_receipt=(ctx, {"lead_created": True} if response is None else response), **changes)
        return lead_id

    def record(self):
        return {"delivery_id": str(uuid4()), "company_id": self.cids[0], "session_id": "session",
                "event_type": "lead_created", "destination_type": "webhook", "payload": {}}

    async def test_unknown_retry_and_restart_replay(self):
        self.assertEqual(await self.status(), {"state": "unknown", "durable": True, "retry_allowed": True})
        claim = await self.begin(retry=True)
        self.assertEqual(claim["state"], "started")
        recreated = PostgresRequestReceipts(self.store)
        self.assertEqual((await recreated.begin(self.cids[0], self.rid, HASH, retry=True))["state"], "processing")
        result = {"message": "saved", "lead_created": False}
        self.assertTrue(await recreated.finish(self.cids[0], self.rid, claim["claim_token"], result))
        self.assertEqual((await self.begin())["response"], result)

    async def test_concurrent_begin_one_owner_even_with_retry(self):
        other = PostgresRequestReceipts(self.store)
        rows = await asyncio.gather(self.begin(), self.begin(retry=True),
                                   other.begin(self.cids[0], self.rid, HASH, retry=True))
        self.assertEqual(sorted(r["state"] for r in rows), ["processing", "processing", "started"])

    async def test_conflict_and_company_scope(self):
        ctx = await self.context()
        await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, {"private": "one"})
        for method in (self.receipts.begin, self.receipts.status):
            with self.assertRaises(RequestConflict):
                await method(self.cids[0], self.rid, OTHER)
        other = await self.receipts.begin(self.cids[1], self.rid, OTHER)
        self.assertEqual(other["state"], "started")
        self.assertFalse(await self.receipts.finish(self.cids[1], self.rid, ctx.claim_token, {}))

    async def test_expired_without_checkpoint_never_reexecutes(self):
        ctx = await self.context()
        await self.expire()
        self.assertEqual((await self.status())["state"], "uncertain")
        self.assertEqual((await self.begin())["state"], "uncertain")
        self.assertEqual((await self.begin(retry=True))["state"], "uncertain")
        self.assertFalse(await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, {}))
        async with self.owner.connect() as conn:
            token = await conn.scalar(sa.select(requests.c.claim_token).where(requests.c.request_id == self.rid))
        self.assertEqual(str(token), ctx.claim_token)

    async def test_failure_is_uncertain_and_cannot_be_reclaimed(self):
        ctx = await self.context()
        await self.receipts.fail(self.cids[0], self.rid, str(uuid4()))
        self.assertEqual((await self.status())["state"], "processing")
        await self.receipts.fail(self.cids[0], self.rid, ctx.claim_token)
        self.assertEqual((await self.begin(retry=True))["state"], "uncertain")
        self.assertFalse(await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, {}))

    async def test_atomic_checkpoint_visible_before_finish_and_after_expiry(self):
        ctx = await self.context()
        record = self.record()
        lead_id = await self.save(ctx, records=[record])
        self.assertEqual((await self.status())["response"], {"lead_created": True})
        async with self.owner.connect() as conn:
            row = (await conn.execute(sa.select(requests).where(requests.c.request_id == self.rid))).mappings().one()
            self.assertEqual((row["status"], row["lead_id"]), ("processing", lead_id))
        await self.expire()
        self.assertEqual((await self.begin(retry=True))["response"], {"lead_created": True})
        self.assertFalse(await self.receipts.finish(self.cids[0], self.rid, str(uuid4()), {}))
        # Final callback semantics can legitimately differ from recovery acknowledgement.
        final = {"lead_created": False, "message": "operator replied"}
        self.assertTrue(await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, final))
        self.assertEqual((await self.status())["response"], final)
        self.assertFalse(await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, {}))
        self.assertEqual(len(await self.store.read_leads(self.cids[0])), 1)
        self.assertEqual(len(await self.store.outbox_records(self.cids[0])), 1)

    async def test_fail_preserves_committed_checkpoint(self):
        ctx = await self.context()
        await self.save(ctx)
        await self.receipts.fail(self.cids[0], self.rid, ctx.claim_token)
        self.assertEqual((await self.begin())["response"], {"lead_created": True})

    async def test_expired_or_foreign_claim_rolls_back_lead_and_outbox(self):
        ctx = await self.context()
        bad = SimpleNamespace(**{**vars(ctx), "claim_token": str(uuid4())})
        for context in (bad, ctx):
            if context is ctx:
                await self.expire()
            with self.assertRaises(ValueError):
                await self.save(context, records=[self.record()])
        self.assertEqual(await self.store.read_leads(self.cids[0]), [])
        self.assertEqual(await self.store.outbox_records(self.cids[0]), [])

    async def test_outbox_failure_rolls_back_lead_and_receipt(self):
        ctx = await self.context()
        bad = {**self.record(), "company_id": self.cids[1]}
        with self.assertRaises(ValueError):
            await self.save(ctx, records=[self.record(), bad])
        self.assertEqual((await self.status())["state"], "processing")
        self.assertEqual(await self.store.read_leads(self.cids[0]), [])
        self.assertEqual(await self.store.outbox_records(self.cids[0]), [])

    async def test_checkpoint_fences_lease_expiring_during_lock_wait(self):
        ctx = await self.context()
        async with self.owner.begin() as conn:
            await conn.execute(requests.update().where(
                requests.c.tenant_id == self.tids[0], requests.c.request_id == self.rid,
            ).values(lease_until=sa.func.clock_timestamp() + sa.text("interval '0.2 second'")))
        async with self.owner.begin() as conn:
            await conn.execute(sa.select(requests).where(
                requests.c.tenant_id == self.tids[0], requests.c.request_id == self.rid,
            ).with_for_update())
            saving = asyncio.create_task(self.save(ctx, records=[self.record()]))
            await asyncio.sleep(0.3)
        with self.assertRaises(ValueError):
            await asyncio.wait_for(saving, timeout=5)
        self.assertEqual(await self.store.read_leads(self.cids[0]), [])
        self.assertEqual(await self.store.outbox_records(self.cids[0]), [])

    async def test_store_tenant_fingerprint_and_transaction_are_checked(self):
        ctx = await self.context()
        foreign = PostgresRequestReceipts(PostgresLeadOutboxStore(self.store.engine))
        for field, value in (("receipts", foreign), ("company_id", self.cids[1]), ("fingerprint", OTHER)):
            with self.assertRaises(ValueError):
                await self.save(SimpleNamespace(**{**vars(ctx), field: value}))
        async with self.owner.begin() as conn:
            # A connection/session from a different engine must not checkpoint this store.
            from sqlalchemy.ext.asyncio import AsyncSession
            async with AsyncSession(bind=conn) as session:
                with self.assertRaises(ValueError):
                    await self.receipts.checkpoint_lead(session, ctx, "lead", {})
        self.assertEqual(await self.store.read_leads(self.cids[0]), [])

    async def test_duplicate_lead_is_compatible_but_second_lead_is_rejected(self):
        ctx = await self.context()
        lead_id = await self.save(ctx)
        await self.save(ctx, lead_id=lead_id)
        with self.assertRaises(ValueError):
            await self.save(ctx)
        self.assertEqual(len(await self.store.read_leads(self.cids[0])), 1)

    async def test_runtime_receipts_privileges_are_minimal(self):
        async with self.store.engine.connect() as conn:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                allowed = await conn.scalar(sa.text("SELECT has_table_privilege(current_user,'chat_requests',:p)"),
                                            {"p": privilege})
                self.assertEqual(allowed, privilege in {"SELECT", "INSERT", "UPDATE"})

    async def test_composite_fk_and_response_constraints(self):
        ctx = await self.context()
        lead_id = await self.save(ctx)
        other = await self.receipts.begin(self.cids[1], self.rid, HASH)
        self.assertEqual(other["state"], "started")
        for values in ({"lead_id": lead_id, "response": {}}, {"status": "done"},
                       {"fingerprint": "raw request"}, {"response": []}, {"lead_id": lead_id}):
            with self.assertRaises(sa.exc.IntegrityError):
                async with self.owner.begin() as conn:
                    await conn.execute(requests.update().where(requests.c.tenant_id == self.tids[1],
                        requests.c.request_id == self.rid).values(**values))

    async def test_invalid_json_does_not_change_receipt(self):
        ctx = await self.context()
        with self.assertRaises(ValueError):
            await self.receipts.finish(self.cids[0], self.rid, ctx.claim_token, {"nan": float("nan")})
        self.assertEqual((await self.status())["state"], "processing")


if __name__ == "__main__":
    unittest.main()
