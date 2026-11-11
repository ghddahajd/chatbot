"""Opt-in unittest; parent's migrated disposable database, never external delivery.

DB_INTEGRATION_TEST=1 TEST_OWNER_DATABASE_URL TEST_LEAD_DATABASE_URL
PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_storage_reports_postgres.py -v
"""

import asyncio
import importlib.util
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

if not importlib.util.find_spec("sqlmodel") or os.environ.get("DB_INTEGRATION_TEST") != "1":
    raise unittest.SkipTest("Requires isolated PostgreSQL with DB_INTEGRATION_TEST=1")

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.db.models import Tenant
from app.db.lead_models import leads, delivery_outbox as outbox
from app.storage.postgres import PostgresLeadOutboxStore
from app.storage.postgres_reports import PostgresReports


class StorageReportsPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = create_async_engine(os.environ["TEST_OWNER_DATABASE_URL"], hide_parameters=True)
        self.store = PostgresLeadOutboxStore(create_async_engine(
            os.environ["TEST_LEAD_DATABASE_URL"], hide_parameters=True))
        self.addAsyncCleanup(self.owner.dispose)
        self.addAsyncCleanup(self.store.close)
        self.cids = ["report_test_" + uuid4().hex for _ in range(2)]
        self.tids = [uuid4(), uuid4()]
        async with self.owner.begin() as conn:
            for cid, tid in zip(self.cids, self.tids):
                await conn.execute(sa.insert(Tenant).values(id=tid, company_id=cid, company_name="Test"))
        self.addAsyncCleanup(self.remove_tenants)
        await self.store.initialize()
        self.reports = self.store.reports

    async def remove_tenants(self):
        async with self.owner.begin() as conn:
            for table in (outbox, leads):
                await conn.execute(table.delete().where(table.c.tenant_id.in_(self.tids)))
            await conn.execute(sa.delete(Tenant).where(Tenant.id.in_(self.tids)))

    async def lead(self, index=0, timestamp="2026-11-01T10:00:00", **changes):
        payload = {"company_id": self.cids[index], "session_id": str(uuid4()), "timestamp": timestamp,
                   "service_id": "consultation", "name": "PRIVATE NAME", "phone": "PRIVATE PHONE",
                   "summary": "PRIVATE SUMMARY", "recent_messages": ["PRIVATE MESSAGE"],
                   "operator_url": "PRIVATE URL", "unresolved_query": "PRIVATE QUERY", **changes}
        await self.store.save_lead(lead_id=str(uuid4()), event_type="lead_created", payload=payload, records=[])
        return payload

    async def event(self, index=0, **changes):
        now = datetime.now(timezone.utc)
        values = {"delivery_id": str(uuid4()), "tenant_id": self.tids[index], "event_type": "lead_created",
                  "session_id": "session", "destination_type": "webhook", "target": "PRIVATE TARGET",
                  "payload": {"phone": "PRIVATE PHONE"}, "created_at": now, "timestamp": now,
                  "next_attempt_at": now + timedelta(hours=1), **changes}
        async with self.owner.begin() as conn:
            await conn.execute(outbox.insert().values(**values))
        return values

    async def test_metadata_is_skinny_and_timestamp_is_utc_naive(self):
        payload = await self.lead(timestamp="2026-11-01T13:00:00+03:00", needs_operator=True)
        await self.lead(1)
        rows = await self.reports.lead_metadata(self.cids[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"timestamp", "company_id", "session_id", "service_id", "reason",
                                       "needs_operator", "lead_trigger", "preferred_time", "page"})
        self.assertEqual(rows[0]["timestamp"], "2026-11-01T10:00:00")
        self.assertEqual(rows[0]["session_id"], payload["session_id"])
        self.assertTrue(rows[0]["needs_operator"])
        self.assertNotIn("PRIVATE", str(rows))
        self.assertEqual({r["company_id"] for r in await self.reports.lead_metadata()
                          if r["company_id"] in self.cids}, set(self.cids))

    async def test_feed_sql_dates_reason_defaults_and_limit(self):
        first = await self.lead(timestamp="2026-11-01T10:00:00", reason=None)
        await self.lead(timestamp="2026-11-02T10:00:00Z", reason="", lead_trigger="")
        await self.lead(timestamp="2026-11-03T13:00:00+03:00", reason="booking_change", needs_operator=True)
        await self.lead(1, timestamp="2026-11-04T10:00:00")
        rows = await self.reports.leads_feed(company_id=self.cids[0], days=-10000,
            start=datetime(2026, 11, 1, 10), end=datetime(2026, 11, 2, 10), reason="commercial_interest")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["session_id"], first["session_id"])
        self.assertEqual(rows[0]["lead_trigger"], "ask_contact")
        self.assertEqual(rows[0]["page"], "")
        self.assertEqual(rows[0]["preferred_time"], "")
        self.assertNotIn("company_id", rows[0])
        self.assertNotIn("PRIVATE", str(rows))
        self.assertEqual(len(await self.reports.leads_feed(company_id=self.cids[0], limit=1)), 1)
        self.assertEqual(await self.reports.leads_feed(company_id=self.cids[0], limit=0), [])
        self.assertEqual(await self.reports.leads_feed(company_id=self.cids[0], days=-10000), [])
        rows = await self.reports.leads_feed(company_id=self.cids[0], reason="booking_change",
            start=datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["needs_operator"])
        for method in (self.reports.leads_feed, self.reports.outbox_summary):
            with self.assertRaises(ValueError):
                await method(limit=-1)

    async def test_malformed_missing_timestamp_uses_structured_time(self):
        when = datetime(2026, 11, 5, tzinfo=timezone.utc)
        async with self.owner.begin() as conn:
            for timestamp in ("invalid", None, 123):
                await conn.execute(leads.insert().values(lead_id=str(uuid4()), tenant_id=self.tids[0],
                    event_type="lead_created", session_id="session", created_at=when,
                    payload={"timestamp": timestamp}))
        rows = await self.reports.leads_feed(company_id=self.cids[0], start=when, end=when)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["timestamp"] for r in rows}, {"2026-11-05T00:00:00"})

    async def test_unknown_tenant_never_falls_back(self):
        for method in (self.reports.lead_metadata, self.reports.leads_feed, self.reports.outbox_summary):
            with self.assertRaises(ValueError):
                await method(company_id="missing_" + uuid4().hex)

    async def test_summary_counts_all_but_limits_recent_and_isolates_tenants(self):
        await self.event(status="pending")
        await self.event(status="sent", destination_type="telegram")
        await self.event(1, status="dead")
        result = await self.reports.outbox_summary(self.cids[0], limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["by_status"], {"pending": 1, "sent": 1})
        self.assertEqual(result["by_destination"], {"webhook": 1, "telegram": 1})
        self.assertEqual(len(result["recent"]), 1)
        self.assertEqual(set(result["recent"][0]), {"timestamp", "delivery_id", "company_id", "session_id",
            "destination_type", "status", "attempts", "next_attempt_at", "last_error", "response_status"})
        self.assertNotIn("PRIVATE", str(result))
        other = await self.reports.outbox_summary(self.cids[1], limit=0)
        self.assertEqual(other["by_status"], {"dead": 1})
        self.assertEqual(other["recent"], [])

    async def test_health_dead_window_future_due_and_expired_lease(self):
        baseline = await self.reports.outbox_health()
        now = datetime.now(timezone.utc)
        await self.event(status="dead", timestamp=now - timedelta(days=8))
        await self.event(1, status="dead", timestamp=now - timedelta(days=6))
        await self.event(status="pending")
        await self.event(status="failed")
        await self.event(status="processing", lease_until=now + timedelta(hours=1), claim_token=uuid4())
        result = await self.reports.outbox_health()
        self.assertEqual(result["pending_events"], baseline["pending_events"] + 3)
        self.assertEqual(result["dead_events"], baseline["dead_events"] + 1)
        self.assertEqual(result["dead_events_total"], baseline["dead_events_total"] + 2)
        self.assertEqual(result["oldest_due_seconds"], baseline["oldest_due_seconds"])
        await self.event(status="processing", lease_until=now - timedelta(seconds=301), claim_token=uuid4())
        result = await self.reports.outbox_health()
        self.assertEqual(result["status"], "degraded")
        self.assertGreaterEqual(result["oldest_due_seconds"], 301)
        self.assertEqual(result["storage"], "postgres")

    async def test_lead_health_count_and_latest_structured_timestamp(self):
        baseline = await self.reports.lead_health()
        await self.lead(timestamp="2099-01-01T00:00:00")
        result = await self.reports.lead_health()
        self.assertEqual(result["count"], baseline["count"] + 1)
        self.assertEqual(result["last_lead_at"], "2099-01-01T00:00:00+00:00")
        self.assertEqual(result["last_lead_company"], self.cids[0])
        self.assertTrue(result["writable"])
        self.assertFalse(result["read_only"])

    async def test_concurrent_health_with_single_connection_pool(self):
        engine = create_async_engine(os.environ["TEST_LEAD_DATABASE_URL"], hide_parameters=True,
                                     pool_size=1, max_overflow=0, pool_timeout=1)
        store = PostgresLeadOutboxStore(engine)
        try:
            results = await asyncio.wait_for(asyncio.gather(
                *[store.reports.lead_health() for _ in range(5)]), timeout=10)
            self.assertTrue(all(result["writable"] for result in results))
        finally:
            await engine.dispose()

    async def test_sql_never_transfers_full_payload_or_delivery_secrets(self):
        await self.lead()
        await self.event()
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        sa.event.listen(self.store.engine.sync_engine, "before_cursor_execute", capture)
        try:
            await self.reports.lead_metadata(self.cids[0])
            await self.reports.leads_feed(company_id=self.cids[0], limit=1)
            await self.reports.outbox_summary(self.cids[0], limit=1)
            await self.reports.lead_health()
            await self.reports.outbox_health()
        finally:
            sa.event.remove(self.store.engine.sync_engine, "before_cursor_execute", capture)
        for statement in statements:
            self.assertNotRegex(statement, r"leads\.payload\s*[,\n]")
            self.assertNotIn("delivery_outbox.payload", statement)
            self.assertNotIn("delivery_outbox.target", statement)
            self.assertNotIn("claim_token", statement)
        self.assertTrue(any("LIMIT" in sql and "ORDER BY leads.created_at DESC" in sql for sql in statements))
        self.assertTrue(any("GROUP BY" in sql for sql in statements))

    async def test_readiness_observes_revocation_and_readonly_without_writes(self):
        # A dedicated NOLOGIN role avoids changing the parent's shared runtime role.
        role = "report_probe_" + uuid4().hex
        async with self.owner.begin() as conn:
            await conn.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
            await conn.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await conn.execute(sa.text(f'GRANT SELECT ON tenants, leads, delivery_outbox, chat_requests TO "{role}"'))
            await conn.execute(sa.text(f'GRANT INSERT ON leads, delivery_outbox, chat_requests TO "{role}"'))
            await conn.execute(sa.text(f'GRANT UPDATE ON delivery_outbox, chat_requests TO "{role}"'))
        async def cleanup():
            async with self.owner.begin() as conn:
                await conn.execute(sa.text(f'DROP OWNED BY "{role}"'))
                await conn.execute(sa.text(f'DROP ROLE "{role}"'))
        self.addAsyncCleanup(cleanup)
        readonly = False
        sessions = async_sessionmaker(self.owner)
        @asynccontextmanager
        async def scoped_session():
            async with sessions() as session, session.begin():
                await session.execute(sa.text(f'SET LOCAL ROLE "{role}"'))
                if readonly:
                    await session.execute(sa.text("SET TRANSACTION READ ONLY"))
                yield session
        reports = PostgresReports(SimpleNamespace(sessions=scoped_session))
        self.assertTrue((await reports.readiness())["writable"])
        async with self.owner.begin() as conn:
            await conn.execute(sa.text(f'REVOKE INSERT ON leads FROM "{role}"'))
        result = await reports.readiness()
        self.assertFalse(result["writable"])
        self.assertIn("leads", result["missing_permissions"])
        self.assertFalse((await reports.lead_health())["writable"])
        async with self.owner.begin() as conn:
            await conn.execute(sa.text(f'GRANT INSERT ON leads TO "{role}"'))
        readonly = True
        result = await reports.readiness()
        self.assertTrue(result["read_only"])
        self.assertFalse(result["writable"])
        readonly = False
        async with self.owner.begin() as conn:
            await conn.execute(sa.text(f'REVOKE SELECT ON chat_requests FROM "{role}"'))
        with self.assertRaises(PermissionError):
            await reports.readiness()


if __name__ == "__main__":
    unittest.main()
