"""Opt-in real PG: DB_INTEGRATION_TEST=1, TEST_OWNER_DATABASE_URL, LEAD_PG*.

TEST_LEAD_DATABASE_URL optionally overrides the LEAD_ connection settings.

Run with: python -m unittest discover -s backend/tests -p test_lead_storage_postgres.py -v
Apply Alembic head first; grant chatbot_lead_runtime via scripts/lead_storage.py.
No HTTP requests or external delivery are performed.
"""

import asyncio
import importlib.util
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

if not importlib.util.find_spec("sqlmodel") or os.environ.get("DB_INTEGRATION_TEST") != "1":
    raise unittest.SkipTest("Requires isolated PostgreSQL with DB_INTEGRATION_TEST=1")

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from app.db.models import Tenant
from app.db.connection import make_engine
from app.db.lead_models import leads, delivery_outbox as outbox
from app.storage.postgres import PostgresLeadOutboxStore
from app.models import Lead, SessionStatus
from app.services.chat_service import ChatService
from app.services.postgres_delivery import PostgresDeliveryService, PostgresLeadService, TELEGRAM_LEAD_CARD
from app.sessions import SessionStore
from app.telegram_bridge import TelegramBridgeService
from app.telegram_routing import TelegramRouting


class LeadStoragePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = create_async_engine(os.environ["TEST_OWNER_DATABASE_URL"], hide_parameters=True)
        runtime_url = os.environ.get("TEST_LEAD_DATABASE_URL")
        runtime_engine = (create_async_engine(runtime_url, hide_parameters=True) if runtime_url
                          else make_engine("LEAD_"))
        self.store = PostgresLeadOutboxStore(runtime_engine)
        self.addAsyncCleanup(self.owner.dispose)
        self.addAsyncCleanup(self.store.close)
        self.cids = ["lead_test_" + uuid4().hex for _ in range(2)]
        self.tids = [uuid4(), uuid4()]
        async with self.owner.begin() as conn:
            for cid, tid in zip(self.cids, self.tids):
                await conn.execute(sa.insert(Tenant).values(id=tid, company_id=cid, company_name="Test"))
        self.addAsyncCleanup(self.remove_tenants)
        await self.store.initialize()

    async def remove_tenants(self):
        async with self.owner.begin() as conn:
            await conn.execute(outbox.delete().where(outbox.c.tenant_id.in_(self.tids)))
            await conn.execute(leads.delete().where(leads.c.tenant_id.in_(self.tids)))
            await conn.execute(sa.delete(Tenant).where(Tenant.id.in_(self.tids)))

    def record(self, index=0, **changes):
        return {"delivery_id": str(uuid4()), "event_type": "lead_created", "company_id": self.cids[index],
                "session_id": "session", "destination_type": "webhook", "target": "https://example.invalid/hook",
                "payload": {"company_id": self.cids[index], "message": "test"}, "status": "pending", "attempts": 0,
                "timestamp": "2026-11-01T10:00:00", **changes}

    def payload(self, index=0):
        return {"company_id": self.cids[index], "session_id": "session", "name": "Test", "timestamp": "2026-11-01T10:00:00"}

    async def expire(self, delivery_id, attempts=None):
        values = {"lease_until": sa.func.clock_timestamp() - sa.text("interval '1 second'")}
        if attempts is not None:
            values["attempts"] = attempts
        async with self.owner.begin() as conn:
            await conn.execute(outbox.update().where(outbox.c.delivery_id == delivery_id).values(**values))

    async def test_atomic_lead_retry_and_conflict(self):
        lead_id, record, payload = str(uuid4()), self.record(), self.payload()
        await self.store.save_lead(lead_id=lead_id, event_type="lead_created", payload=payload, records=[record])
        await self.store.save_lead(lead_id=lead_id, event_type="lead_created", payload=payload,
                                   records=[{**record, "delivery_id": str(uuid4())}])
        self.assertEqual(await self.store.read_leads(self.cids[0]), [payload])
        rows = await self.store.outbox_records(self.cids[0])
        self.assertEqual([r["delivery_id"] for r in rows], [record["delivery_id"]])
        for changed in ({**payload, "name": "Other"}, self.payload(1)):
            with self.assertRaises(ValueError):
                await self.store.save_lead(lead_id=lead_id, event_type="lead_created", payload=changed, records=[])

    async def test_bridge_delivers_saved_leads_after_restart(self):
        with TemporaryDirectory() as temp:
            pending = Path(temp) / "pending.jsonl"
            routing = TelegramRouting(client_groups_raw=json.dumps({
                self.cids[0]: {"group": "-1001", "topic": "11"},
                self.cids[1]: {"group": "-1002", "topic": "22"},
            }))
            resolver = SimpleNamespace(get=lambda *args, **kwargs: None)

            def bridge(sessions):
                service = TelegramBridgeService(bot_token="stub", group_chat_id="", session_store=sessions,
                                                ws_manager=None, routing=routing, pending_file=pending)
                service._call = AsyncMock()
                return service

            old_sessions = SessionStore()
            old_bridge = bridge(old_sessions)
            delivery = PostgresDeliveryService(store=self.store, knowledge_base_resolver=resolver)
            delivery._destinations_for = lambda **kwargs: []
            leads_service = PostgresLeadService(store=self.store, delivery_service=delivery)
            chat = ChatService(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                postgres_lead_store=self.store, lead_service=leads_service,
                telegram_bridge_service=old_bridge, session_store=old_sessions,
            ))))
            first = await old_sessions.get_or_create(None, self.cids[0])
            second = await old_sessions.get_or_create(None, self.cids[1])
            set_lead_requested = old_sessions.set_lead_requested
            old_sessions.set_lead_requested = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await chat._save_chat_lead(Lead(company_id=self.cids[0], session_id=first.session_id,
                    name="First", phone="+70000000001", summary="Test", needs_operator=True), reason="Lead")
            old_sessions.set_lead_requested = set_lead_requested
            second_lead = Lead(company_id=self.cids[1], session_id=second.session_id,
                               name="Second", phone="+70000000002", summary="Test", needs_operator=True)
            for _ in range(2):
                await chat._save_chat_lead(second_lead, reason="Lead")
            old_bridge._call.assert_not_awaited()
            await self.store.close()

            runtime_url = os.environ.get("TEST_LEAD_DATABASE_URL")
            engine = create_async_engine(runtime_url, hide_parameters=True) if runtime_url else make_engine("LEAD_")
            restarted_store = PostgresLeadOutboxStore(engine)
            self.addAsyncCleanup(restarted_store.close)
            restarted = PostgresDeliveryService(store=restarted_store, knowledge_base_resolver=resolver)
            # Восстановился только второй диалог.
            restored = SessionStore()
            await restored.get_or_create(second.session_id, self.cids[1])
            await restored.set_status(second.session_id, SessionStatus.WAITING_OPERATOR)
            restarted.telegram_bridge = bridge(restored)
            send = restarted.telegram_bridge._call
            send.side_effect = [{"ok": False, "error_code": 429}, TimeoutError(), {"ok": True}, {"ok": True}]
            for _ in range(2):
                result = await restarted.retry_due(company_id=self.cids[0])
                self.assertEqual(result["failed"], 1)
                self.assertFalse(pending.exists())
                async with self.owner.begin() as conn:
                    await conn.execute(outbox.update().where(outbox.c.tenant_id == self.tids[0])
                                       .values(next_attempt_at=sa.func.now()))
            self.assertEqual((await restarted.retry_due(company_id=self.cids[0]))["sent"], 1)
            self.assertEqual((await restarted.retry_due(company_id=self.cids[1]))["sent"], 1)
            self.assertEqual(await restarted.retry_due(), {"attempted": 0, "sent": 0, "failed": 0, "dead": 0})
            fallback, throttled_retry, timeout_retry, live = [call.kwargs for call in send.await_args_list]
            self.assertEqual(fallback, throttled_retry)
            self.assertEqual(fallback, timeout_retry)
            self.assertEqual((fallback["chat_id"], fallback["message_thread_id"]), ("-1001", 11))
            self.assertIn("70000000001", fallback["text"])
            self.assertNotIn("reply_markup", fallback)
            self.assertEqual(live["chat_id"], "-1002")
            self.assertEqual(live["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                             f"claim:{second.session_id}")
            self.assertFalse(pending.exists())
            for cid in self.cids:
                self.assertEqual(len(await restarted_store.read_leads(cid)), 1)
                rows = await restarted_store.outbox_records(cid)
                self.assertEqual([(r["destination_type"], r["status"]) for r in rows], [(TELEGRAM_LEAD_CARD, "sent")])

    async def test_cross_tenant_and_unknown_roll_back_everything(self):
        for bad in (self.record(1), self.record(company_id="missing_" + uuid4().hex)):
            with self.assertRaises(ValueError):
                await self.store.save_lead(lead_id=str(uuid4()), event_type="lead_created",
                                           payload=self.payload(), records=[self.record(), bad])
        self.assertEqual(await self.store.read_leads(self.cids[0]), [])
        self.assertEqual(await self.store.outbox_records(self.cids[0]), [])
        with self.assertRaises(ValueError):
            await self.store.enqueue([self.record(), self.record(company_id="unknown")])
        self.assertEqual(await self.store.outbox_records(self.cids[0]), [])
        for method in (self.store.read_leads, self.store.outbox_records):
            with self.assertRaises(ValueError):
                await method("unknown")
        with self.assertRaises(ValueError):
            await self.store.claim(company_id="unknown")

    async def test_generic_idempotency_and_tenant_scoping(self):
        record, other = self.record(event_type="operator_requested"), self.record(1)
        await self.store.enqueue([record, other])
        await self.store.enqueue([record])
        with self.assertRaises(ValueError):
            await self.store.enqueue([{**record, "target": "changed"}])
        rows = await self.store.outbox_records(self.cids[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "operator_requested")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertNotIn("claim_token", rows[0])
        await self.store.claim(company_id=self.cids[0])
        self.assertEqual((await self.store.outbox_records(self.cids[1]))[0]["attempts"], 0)

    async def test_concurrent_claims_do_not_overlap(self):
        await self.store.enqueue([self.record() for _ in range(8)])
        first, second = await asyncio.gather(self.store.claim(company_id=self.cids[0], limit=4),
                                            self.store.claim(company_id=self.cids[0], limit=4))
        self.assertEqual(len(first + second), 8)
        self.assertEqual(len({r["delivery_id"] for r in first + second}), 8)
        self.assertTrue(all(r["attempts"] == 1 and r["status"] == "processing" for r in first + second))
        self.assertTrue(all(r["lease_until"] is not None for r in first + second))

    async def test_skip_locked_does_not_wait(self):
        first, second = self.record(), self.record()
        await self.store.enqueue([first, second])
        async with self.owner.begin() as conn:
            await conn.execute(sa.select(outbox).where(outbox.c.delivery_id == first["delivery_id"]).with_for_update())
            claimed = await asyncio.wait_for(self.store.claim(company_id=self.cids[0], limit=1), timeout=3)
            self.assertEqual(claimed[0]["delivery_id"], second["delivery_id"])

    async def test_expired_lease_reclaims_and_fences_old_worker(self):
        await self.store.enqueue([self.record()])
        old = (await self.store.claim(company_id=self.cids[0]))[0]
        await self.expire(old["delivery_id"])
        self.assertFalse(await self.store.finish({**old, "status": "sent"}))
        current = (await self.store.claim(company_id=self.cids[0]))[0]
        self.assertEqual(current["attempts"], 2)
        self.assertNotEqual(current["claim_token"], old["claim_token"])
        self.assertFalse(await self.store.finish({**old, "status": "sent"}))
        self.assertTrue(await self.store.finish({**current, "status": "sent", "response_status": 200}))
        self.assertFalse(await self.store.finish({**current, "status": "sent"}))
        row = (await self.store.outbox_records(self.cids[0]))[0]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["event_timestamp"], "2026-11-01T10:00:00+00:00")
        self.assertEqual(row["created_at"], row["event_timestamp"])
        self.assertNotEqual(row["timestamp"], row["event_timestamp"])
        self.assertNotIn("claim_token", row)
        self.assertNotIn("claim_token", row["payload"])

    async def test_fifth_attempt_expiry_becomes_dead(self):
        await self.store.enqueue([self.record()])
        row = (await self.store.claim(company_id=self.cids[0]))[0]
        await self.expire(row["delivery_id"], attempts=5)
        self.assertEqual(await self.store.claim(company_id=self.cids[0]), [])
        row = (await self.store.outbox_records(self.cids[0]))[0]
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("dead", 5, "lease_expired"))

    async def test_exhausted_cleanup_does_not_hide_next_delivery(self):
        await self.store.enqueue([self.record()])
        first = (await self.store.claim(company_id=self.cids[0], limit=1))[0]
        await self.expire(first["delivery_id"], attempts=5)
        second = self.record(timestamp="2026-11-02T10:00:00")
        await self.store.enqueue([second])
        claimed = await self.store.claim(company_id=self.cids[0], limit=1)
        self.assertEqual([row["delivery_id"] for row in claimed], [second["delivery_id"]])

    async def test_backoff_due_and_maximum_attempts(self):
        await self.store.enqueue([self.record()])
        for attempt in range(1, 6):
            row = (await self.store.claim(company_id=self.cids[0]))[0]
            self.assertEqual(row["attempts"], attempt)
            self.assertTrue(await self.store.finish({**row, "status": "failed", "response_status": 503,
                                                     "last_error": "http_status_503",
                                                     "next_attempt_at": "2099-01-01T00:00:00+00:00"}))
            self.assertEqual(await self.store.claim(company_id=self.cids[0]), [])
            async with self.owner.begin() as conn:
                await conn.execute(outbox.update().where(outbox.c.delivery_id == row["delivery_id"])
                                   .values(next_attempt_at=datetime(2000, 1, 1, tzinfo=timezone.utc)))
        final = (await self.store.outbox_records(self.cids[0]))[0]
        self.assertEqual((final["status"], final["attempts"]), ("dead", 5))

    async def test_runtime_permissions_and_no_auth_access(self):
        async with self.store.engine.connect() as conn:
            for table, forbidden in {"tenants": ["INSERT", "UPDATE", "DELETE"],
                                     "leads": ["UPDATE", "DELETE", "TRUNCATE"],
                                     "delivery_outbox": ["DELETE", "TRUNCATE"],
                                     "client_accounts": ["SELECT", "INSERT", "UPDATE", "DELETE"],
                                     "client_login_sessions": ["SELECT", "INSERT", "UPDATE", "DELETE"]}.items():
                for privilege in forbidden:
                    self.assertFalse(await conn.scalar(sa.text(
                        "SELECT has_table_privilege(current_user,:table,:privilege)"),
                        {"table": table, "privilege": privilege}))
            self.assertFalse(await conn.scalar(sa.text("SELECT has_schema_privilege(current_user,'public','CREATE')")))
        with self.assertRaises(RuntimeError):
            await PostgresLeadOutboxStore(self.owner).initialize()

    async def test_composite_fk_rejects_cross_tenant_reference(self):
        lead_id = str(uuid4())
        await self.store.save_lead(lead_id=lead_id, event_type="lead_created", payload=self.payload(), records=[])
        with self.assertRaises(sa.exc.IntegrityError):
            async with self.owner.begin() as conn:
                await conn.execute(outbox.insert().values(delivery_id=str(uuid4()), tenant_id=self.tids[1],
                    lead_id=lead_id, event_type="lead_created", session_id="session",
                    destination_type="webhook", payload={}))

    async def test_unregistered_active_kb_blocks_activation(self):
        await self.store.validate_companies(self.cids)
        with self.assertRaises(ValueError):
            await self.store.validate_companies([*self.cids, "not_registered"])

    async def test_half_lease_is_rejected_by_database(self):
        with self.assertRaises(sa.exc.IntegrityError):
            async with self.owner.begin() as conn:
                await conn.execute(outbox.insert().values(delivery_id=str(uuid4()), tenant_id=self.tids[0],
                    event_type="operator_requested", session_id="session", destination_type="webhook",
                    payload={}, lease_until=datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
