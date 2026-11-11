"""Native unittest SQL-shape checks; skip hosts without optional SQL dependencies."""

import importlib.util
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

if not importlib.util.find_spec("sqlmodel"):
    raise unittest.SkipTest("Optional SQL dependencies are not installed")

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from app.db.lead_models import leads
from app.storage.postgres_reports import PostgresReports, _timestamp


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        return self.rows[0]

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class StorageReportsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.statements = []
        self.results = []
        self.scalars = []
        self.open_sessions = 0
        self.max_sessions = 0
        test = self
        class Session:
            async def execute(self, query):
                test.statements.append(query)
                return Result(test.results.pop(0))

            async def scalar(self, query):
                test.statements.append(query)
                return test.scalars.pop(0)
        @asynccontextmanager
        async def sessions():
            self.open_sessions += 1
            self.max_sessions = max(self.max_sessions, self.open_sessions)
            try:
                yield Session()
            finally:
                self.open_sessions -= 1
        self.store = SimpleNamespace(sessions=sessions, _tenant=AsyncMock(return_value="tenant-id"))
        self.reports = PostgresReports(self.store)

    def sql(self, index=-1):
        return str(self.statements[index].compile(dialect=postgresql.dialect()))

    async def test_feed_projects_native_scalars_and_applies_bounded_sql_filters(self):
        self.results = [[]]
        await self.reports.leads_feed(company_id="company", start=datetime(2026, 1, 1),
                                     end=datetime(2026, 2, 1), reason="commercial_interest", limit=7)
        sql = self.sql()
        self.assertIn("->>", sql)
        self.assertNotIn(" -> ", sql)
        self.assertIn("AS BOOLEAN", sql)
        self.assertIn("leads.tenant_id =", sql)
        self.assertIn("leads.created_at >=", sql)
        self.assertIn("leads.created_at <=", sql)
        self.assertIn("coalesce(nullif(", sql)
        self.assertIn("ORDER BY leads.created_at DESC", sql)
        self.assertIn("LIMIT", sql)
        self.assertIn(7, self.statements[0].compile().params.values())
        self.assertTrue(all(column is not leads.c.payload for column in self.statements[0].selected_columns))
        for field in ("phone", "name", "summary", "recent_messages", "operator_url", "unresolved_query"):
            self.assertNotIn(field, self.statements[0].compile().params.values())

    async def test_summary_sql_aggregates_and_recent_has_no_secrets(self):
        self.results = [[("pending", 3)], [("webhook", 3)], []]
        result = await self.reports.outbox_summary(company_id="company", limit=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["recent"], [])
        self.assertIn("GROUP BY delivery_outbox.status", self.sql(0))
        self.assertIn("GROUP BY delivery_outbox.destination_type", self.sql(1))
        self.assertIn("LIMIT", self.sql(2))
        for statement in self.statements:
            for column in statement.selected_columns:
                self.assertNotIn(column.name, ("payload", "target", "claim_token"))

    async def test_health_is_count_min_only_and_floors_due_age(self):
        self.results = [[{"pending_events": 2, "dead_events": 0, "dead_events_total": 4,
                          "oldest_due_seconds": 300.9}]]
        result = await self.reports.outbox_health()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["oldest_due_seconds"], 300)
        sql = self.sql()
        for expression in ("count(*) FILTER", "min(CASE", "delivery_outbox.lease_until", "greatest("):
            self.assertIn(expression, sql)
        self.assertNotIn("payload", sql)

    def permissions(self, denied=None):
        return [{"table_name": table, "privilege": privilege, "allowed": (table, privilege) != denied}
                for table, privileges in (("tenants", ("SELECT",)), ("leads", ("SELECT", "INSERT")),
                    ("delivery_outbox", ("SELECT", "INSERT", "UPDATE")),
                    ("chat_requests", ("SELECT", "INSERT", "UPDATE"))) for privilege in privileges]

    async def test_readiness_after_revoke_and_readonly_never_uses_writes(self):
        for denied, mode in ((None, "off"), (("leads", "INSERT"), "off"), (None, "on")):
            self.results = [self.permissions(denied)]
            self.scalars = [mode]
            result = await self.reports.readiness()
            self.assertEqual(result["writable"], denied is None and mode == "off")
            self.assertEqual(result["read_only"], mode == "on")
        for statement in self.statements:
            sql = str(statement).lstrip()
            self.assertTrue(sql.startswith(("SELECT", "SHOW")))
            self.assertNotIn("payload", sql)

    async def test_missing_select_or_database_error_propagates(self):
        self.results = [self.permissions(("chat_requests", "SELECT"))]
        self.scalars = ["off"]
        with self.assertRaises(PermissionError):
            await self.reports.readiness()
        self.store.sessions = unittest.mock.Mock(side_effect=OSError("database unavailable"))
        with self.assertRaises(OSError):
            await self.reports.readiness()

    async def test_lead_health_closes_readiness_session_before_count_latest(self):
        self.results = [self.permissions(), []]
        self.scalars = ["off", 0]
        result = await self.reports.lead_health()
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["last_lead_at"])
        self.assertIsNone(result["last_lead_company"])
        self.assertEqual(self.max_sessions, 1)
        self.assertEqual(self.open_sessions, 0)
        self.assertIn("count(*)", self.sql(2))
        self.assertIn("LIMIT", self.sql(3))
        self.assertNotIn("payload", self.sql(3))

    def test_timestamp_naive_preserved_aware_and_fallback_normalized(self):
        fallback = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.assertEqual(_timestamp("2026-11-01T10:00:00", fallback), "2026-11-01T10:00:00")
        self.assertEqual(_timestamp("2026-11-01T13:00:00+03:00", fallback), "2026-11-01T10:00:00")
        self.assertEqual(_timestamp("2026-11-01T10:00:00Z", fallback), "2026-11-01T10:00:00")
        for malformed in (None, "bad", "", 123):
            self.assertEqual(_timestamp(malformed, fallback), "2026-11-05T00:00:00")


if __name__ == "__main__":
    unittest.main()
