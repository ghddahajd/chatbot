"""Native unittest checks: no database dependencies or network required."""

import asyncio
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.storage.request_receipts import MemoryRequestReceipts, RequestConflict


HASH = sha256(b"request").hexdigest()
OTHER = sha256(b"other").hexdigest()


class MemoryRequestReceiptsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch("app.storage.request_receipts.monotonic", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.store = MemoryRequestReceipts()
        self.rid = str(uuid4())

    async def begin(self, **kwargs):
        return await self.store.begin("one", self.rid, HASH, **kwargs)

    async def status(self):
        return await self.store.status("one", self.rid, HASH)

    async def context(self):
        claim = await self.begin()
        return SimpleNamespace(receipts=self.store, company_id="one", request_id=self.rid,
                               fingerprint=HASH, claim_token=claim["claim_token"])

    async def test_unknown_retry_is_never_executed(self):
        expected = {"state": "unknown", "durable": False, "retry_allowed": False}
        self.assertEqual(await self.status(), expected)
        self.assertEqual(await self.begin(retry=True), expected)
        self.assertEqual(len(self.store._rows), 0)

    async def test_concurrent_begin_has_one_owner(self):
        rows = await asyncio.gather(*(self.begin() for _ in range(10)))
        self.assertEqual(sum(r["state"] == "started" for r in rows), 1)
        self.assertEqual(sum(r["state"] == "processing" for r in rows), 9)
        self.assertTrue(all(not r["durable"] for r in rows))

    async def test_finish_and_replay_are_copied_and_fenced(self):
        ctx = await self.context()
        result = {"message": "saved", "nested": {"lead": "one"}}
        self.assertFalse(await self.store.finish("one", self.rid, str(uuid4()), result))
        self.assertTrue(await self.store.finish("one", self.rid, ctx.claim_token, result))
        result["nested"]["lead"] = "changed"
        replay = await self.begin(retry=True)
        self.assertEqual(replay["response"]["nested"]["lead"], "one")
        replay["response"]["nested"]["lead"] = "changed again"
        self.assertEqual((await self.status())["response"]["nested"]["lead"], "one")
        self.assertFalse(await self.store.finish("one", self.rid, ctx.claim_token, {}))
        await self.store.fail("one", self.rid, ctx.claim_token)
        self.assertEqual((await self.status())["state"], "done")

    async def test_fingerprint_conflict_never_reveals_result(self):
        ctx = await self.context()
        await self.store.finish("one", self.rid, ctx.claim_token, {"private": "value"})
        for method in (self.store.begin, self.store.status):
            with self.assertRaises(RequestConflict):
                await method("one", self.rid, OTHER)

    async def test_same_id_is_separate_per_company(self):
        first = await self.context()
        other = await self.store.begin("two", self.rid, OTHER)
        self.assertEqual(other["state"], "started")
        self.assertFalse(await self.store.finish("two", self.rid, first.claim_token, {}))
        self.assertEqual((await self.status())["state"], "processing")

    async def test_expiry_is_uncertain_without_takeover(self):
        ctx = await self.context()
        self.now.return_value = 1300
        for retry in (False, True):
            self.assertEqual((await self.begin(retry=retry))["state"], "uncertain")
        self.assertEqual((await self.status())["state"], "uncertain")
        self.assertFalse(await self.store.finish("one", self.rid, ctx.claim_token, {}))
        with self.assertRaises(ValueError):
            self.store.checkpoint_ready(ctx)

    async def test_fail_preserves_checkpoint_and_fences_foreign_token(self):
        ctx = await self.context()
        await self.store.fail("one", self.rid, str(uuid4()))
        self.assertEqual((await self.status())["state"], "processing")
        self.store.checkpoint_ready(ctx)
        self.store.checkpoint_lead(ctx, "lead-one", {"message": "saved"})
        await self.store.fail("one", self.rid, ctx.claim_token)
        self.assertEqual((await self.status())["response"], {"message": "saved"})
        self.assertFalse(await self.store.finish("one", self.rid, ctx.claim_token, {}))

    async def test_callback_failure_never_allows_reexecution(self):
        ctx = await self.context()
        await self.store.fail("one", self.rid, ctx.claim_token)
        self.assertEqual((await self.begin())["state"], "uncertain")
        self.assertEqual((await self.begin(retry=True))["state"], "uncertain")

    async def test_checkpoint_visible_then_final_finish_after_expiry(self):
        ctx = await self.context()
        self.now.return_value = 1299.999
        self.store.checkpoint_ready(ctx)
        # Synchronous append crosses the lease boundary without event-loop interleaving.
        self.now.return_value = 1300.001
        self.store.checkpoint_lead(ctx, "lead-one", {"message": "recovered"})
        self.assertEqual((await self.begin())["response"], {"message": "recovered"})
        self.assertTrue(await self.store.finish("one", self.rid, ctx.claim_token, {"message": "final"}))
        self.assertEqual((await self.status())["response"], {"message": "final"})

    async def test_checkpoint_checks_identity_before_append(self):
        ctx = await self.context()
        for field, value in (("fingerprint", OTHER), ("claim_token", str(uuid4())),
                             ("company_id", "two"), ("receipts", MemoryRequestReceipts())):
            bad = SimpleNamespace(**{**vars(ctx), field: value})
            with self.assertRaises(ValueError):
                self.store.checkpoint_ready(bad)
            with self.assertRaises(ValueError):
                self.store.checkpoint_lead(bad, "lead", {})
        self.assertEqual((await self.status())["state"], "processing")

    async def test_final_response_is_exact_even_if_recovery_ack_differs(self):
        ctx = await self.context()
        self.store.checkpoint_ready(ctx)
        self.store.checkpoint_lead(ctx, "lead-one", {"lead_created": True})
        final = {"lead_created": False, "message": "operator replied"}
        self.assertTrue(await self.store.finish("one", self.rid, ctx.claim_token, final))
        self.assertEqual((await self.status())["response"], final)

    async def test_capacity_protects_active_claim_and_checkpoint(self):
        self.store = MemoryRequestReceipts(max_entries=1)
        ctx = await self.context()
        self.store.checkpoint_ready(ctx)
        self.store.checkpoint_lead(ctx, "lead-one", {})
        with self.assertRaises(RuntimeError):
            await self.store.begin("one", str(uuid4()), HASH)
        self.assertEqual((await self.status())["response"], {})
        await self.store.finish("one", self.rid, ctx.claim_token, {})
        self.assertEqual((await self.store.begin("one", str(uuid4()), HASH))["state"], "started")
        self.assertEqual((await self.begin(retry=True))["state"], "unknown")
        self.assertEqual(len(self.store._rows), 1)

    async def test_ttl_eviction_and_restart_cannot_retry(self):
        ctx = await self.context()
        await self.store.finish("one", self.rid, ctx.claim_token, {})
        self.now.return_value = 4600
        self.assertFalse((await self.status())["retry_allowed"])
        self.assertEqual((await self.begin(retry=True))["state"], "unknown")
        self.store = MemoryRequestReceipts()
        self.assertEqual((await self.begin(retry=True))["state"], "unknown")

    async def test_short_ttl_does_not_evict_active_processing(self):
        self.store = MemoryRequestReceipts(ttl_seconds=1)
        await self.begin()
        self.now.return_value = 1002
        self.assertEqual((await self.status())["state"], "processing")

    async def test_invalid_inputs_are_rejected(self):
        for args in (("../one", self.rid, HASH), ("one", "not-uuid", HASH),
                     ("one", self.rid, "raw request")):
            with self.assertRaises(ValueError):
                await self.store.begin(*args)
        ctx = await self.context()
        for result in ([], {"nan": float("nan")}, {"value": object()}):
            with self.assertRaises(ValueError):
                await self.store.finish("one", self.rid, ctx.claim_token, result)
        self.assertEqual((await self.status())["state"], "processing")


if __name__ == "__main__":
    unittest.main()
