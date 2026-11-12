"""Runtime integration without a database, credentials or external delivery."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.analytics import AnalyticsService
from app.config import Settings, get_settings
from app.health_checks import collect_health_checks
from app.ops_bot import OpsBot
from app.preflight import _collect_sync, check_leads, run_preflight
from app.storage.errors import LeadStorageError


class Store:
    def __init__(self):
        self.reports = self
        self.calls = []
        self.rows = [{"company_id": "dup_one", "timestamp": "2026-11-10T12:00:00", "reason": "booking_request"}]

    async def read_leads(self, company_id=None):
        self.calls.append(company_id)
        return [r for r in self.rows if company_id is None or r["company_id"] == company_id]

    async def lead_metadata(self, company_id=None):
        return await self.read_leads(company_id)

    async def leads_feed(self, company_id=None, **filters):
        return await self.read_leads(company_id)

    async def lead_health(self):
        return dict(count=len(self.rows), last_lead_at="2026-11-10T12:00:00Z",
                    last_lead_company="dup_one", writable=True, read_only=False)

    async def close(self):
        pass


def attach_store(client, monkeypatch):
    store = Store()
    monkeypatch.setattr(client.app.state.analytics_service, "postgres_lead_store", store)
    monkeypatch.setattr(client.app.state, "postgres_lead_store", store)
    monkeypatch.setattr(client.app.state.settings, "lead_storage_backend", "postgres")

    def no_files():
        raise AssertionError("must not read file leads")

    monkeypatch.setattr(client.app.state.analytics_service.lead_store, "read_all", no_files)
    return store


def test_snapshot_is_scoped_to_service_and_restored(tmp_path):
    store = Store()
    service = AnalyticsService(tmp_path / "events", tmp_path / "leads", postgres_lead_store=store)
    other = AnalyticsService(tmp_path / "events", tmp_path / "leads", postgres_lead_store=store)
    with pytest.raises(LeadStorageError):
        service._all_leads()
    async def check():
        async with service.leads_snapshot("dup_one"):
            assert await asyncio.to_thread(service._all_leads) == store.rows
            with pytest.raises(LeadStorageError):
                other._all_leads()
            with pytest.raises(ValueError):
                async with service.leads_snapshot("dup_two"):
                    assert service._all_leads() == []
                    raise ValueError("test unwind")
            assert service._all_leads() == store.rows

    asyncio.run(check())
    with pytest.raises(LeadStorageError):
        service._all_leads()
    assert store.calls == ["dup_one", "dup_two"]


@pytest.mark.parametrize("path", ["summary", "operators", "dashboard", "leads"])
def test_authorization_before_read_and_owner_scope(test_client, monkeypatch, path):
    store = attach_store(test_client, monkeypatch)
    assert test_client.get(f"/api/analytics/{path}").status_code == 403
    assert store.calls == []
    response = test_client.get(f"/api/analytics/{path}?token=demo-operator-token&company_id=dup_one")
    assert response.status_code == 200, response.text
    assert store.calls == ["dup_one"]
    assert test_client.get(f"/api/analytics/{path}?token=demo-operator-token").status_code == 200
    assert store.calls == ["dup_one", None]


def test_client_scope_is_enforced_before_pg_read(test_client, monkeypatch):
    from app.auth import CLIENT_COOKIE_NAME, Principal

    store = attach_store(test_client, monkeypatch)
    monkeypatch.setattr(test_client.app.state.settings, "client_login_enabled", True)
    access = SimpleNamespace(authenticate=AsyncMock(return_value=Principal(owner=False, company_id="dup_one")),
                             close=AsyncMock())
    monkeypatch.setattr(test_client.app.state, "client_access", access)
    test_client.cookies.set(CLIENT_COOKIE_NAME, "test-session")
    assert test_client.get("/api/analytics/leads?company_id=dup_two").status_code == 403
    assert store.calls == []
    assert test_client.get("/api/analytics/leads").status_code == 200
    assert store.calls == ["dup_one"]


def test_read_error_returns_safe_503(test_client, monkeypatch):
    store = attach_store(test_client, monkeypatch)
    store.reports.lead_metadata = AsyncMock(side_effect=RuntimeError("private diagnostic"))
    response = test_client.get("/api/analytics/dashboard?token=demo-operator-token")
    assert response.status_code == 503
    assert response.json() == {"detail": "Lead storage unavailable"}


def test_pg_leads_feed_uses_sql_filters_without_snapshot_or_runner(test_client, monkeypatch):
    from datetime import datetime, time

    store = attach_store(test_client, monkeypatch)
    store.reports.lead_metadata = AsyncMock(side_effect=AssertionError("metadata not needed"))
    store.reports.leads_feed = AsyncMock(return_value=[])
    monkeypatch.setattr(test_client.app.state.analytics_service.runner, "report",
                        AsyncMock(side_effect=AssertionError("SQL feed must not use CPU runner")))
    response = test_client.get(
        "/api/analytics/leads?company_id=dup_one&days=7&start_date=2026-01-01&end_date=2026-01-02"
        "&reason=booking_change&limit=12", headers={"x-operator-token": "demo-operator-token"},
    )
    assert response.status_code == 200, response.text
    store.reports.leads_feed.assert_awaited_once_with(
        company_id="dup_one", days=7, start=datetime(2026, 1, 1),
        end=datetime.combine(datetime(2026, 1, 2).date(), time.max), reason="booking_change", limit=12,
    )
    store.reports.lead_metadata.assert_not_awaited()
    store.reports.leads_feed.side_effect = RuntimeError("private diagnostic")
    response = test_client.get("/api/analytics/leads?token=demo-operator-token")
    assert response.status_code == 503
    assert response.json() == {"detail": "Lead storage unavailable"}


@pytest.mark.parametrize("command", ["_cmd_today", "_cmd_leads", "_cmd_funnel"])
def test_ops_commands_use_snapshot(test_client, monkeypatch, command):
    store = attach_store(test_client, monkeypatch)
    bot = OpsBot(test_client.app, SimpleNamespace(project="Test"))
    result = test_client.portal.call(getattr(bot, command))
    assert isinstance(result, str)
    assert store.calls == [None]


def test_pg_health_and_preflight_never_use_file_checks(test_client, monkeypatch):
    store = attach_store(test_client, monkeypatch)
    delivery = SimpleNamespace(get_health=AsyncMock(return_value={"status": "degraded", "dead_events": 3}))
    monkeypatch.setattr(test_client.app.state, "delivery_service", delivery)
    assert collect_health_checks(test_client.app)["delivery"]["status"] == "error"
    assert check_leads(test_client.app.state.settings)["status"] == "error"
    assert _collect_sync(test_client.app)["leads"]["status"] == "error"
    response = test_client.get("/health")
    assert response.json()["checks"]["delivery"]["dead_events"] == 3
    result = test_client.portal.call(lambda: run_preflight(test_client.app, include_network=False))
    leads = result["checks"]["leads"]
    assert leads["source"] == "postgres" and leads["count"] == 1
    assert leads["last_lead_at"] == "2026-11-10T12:00:00Z"
    assert leads["writable"] is True
    archive = next(t for t in result["checks"]["background_tasks"]["tasks"] if t["name"] == "leads_archive")
    assert archive["status"] == "skip"
    delivery.get_health.side_effect = LeadStorageError("private diagnostic")
    store.reports.lead_health = AsyncMock(side_effect=LeadStorageError("private diagnostic"))
    assert test_client.get("/health").status_code == 503
    result = test_client.portal.call(lambda: run_preflight(test_client.app, include_network=False))
    assert result["checks"]["delivery"]["status"] == "error"
    assert result["checks"]["leads"]["status"] == "error"
    assert "private diagnostic" not in str(result)


@pytest.fixture
def fake_pg_modules(monkeypatch):
    store = SimpleNamespace(initialize=AsyncMock(), validate_companies=AsyncMock(), close=AsyncMock())
    engine_calls = []
    connection = ModuleType("app.db.connection")
    connection.make_engine = lambda prefix="": engine_calls.append(prefix)
    postgres = ModuleType("app.storage.postgres")
    postgres.PostgresLeadOutboxStore = lambda engine: store
    receipts = ModuleType("app.storage.postgres_receipts")
    receipts.PostgresRequestReceipts = lambda store: SimpleNamespace()
    adapters = ModuleType("app.services.postgres_delivery")
    adapters.PostgresDeliveryService = lambda **kwargs: SimpleNamespace(**kwargs)
    adapters.PostgresLeadService = lambda **kwargs: SimpleNamespace(**kwargs)
    for module in (connection, postgres, adapters, receipts):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return store, engine_calls


def test_pg_lifespan_wires_and_closes_after_workers(managed_env, monkeypatch, fake_pg_modules):
    from fastapi.testclient import TestClient
    from app.main import app

    store, engine_calls = fake_pg_modules
    worker_stopped = []

    async def worker(interval):
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.append(True)

    def closed():
        assert worker_stopped

    store.close.side_effect = closed
    module = sys.modules["app.services.postgres_delivery"]
    module.PostgresDeliveryService = lambda **kwargs: SimpleNamespace(run_retry_loop=worker, **kwargs)
    monkeypatch.setenv("LEAD_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DELIVERY_RETRY_ENABLED", "true")
    get_settings.cache_clear()
    with TestClient(app):
        assert app.state.analytics_service.postgres_lead_store is store
        assert app.state.lead_service.store is store
        assert app.state.delivery_service.store is store
        assert "leads_archive" not in app.state.background_tasks
    assert engine_calls == ["LEAD_"]
    store.initialize.assert_awaited_once()
    store.validate_companies.assert_awaited_once()
    store.close.assert_awaited_once()


def test_pg_startup_fails_closed_and_safely(managed_env, monkeypatch, fake_pg_modules, caplog):
    from fastapi.testclient import TestClient
    from app.main import app

    store, _ = fake_pg_modules
    error = RuntimeError("private diagnostic")
    error.sqlstate = "28000"
    store.initialize.side_effect = error
    monkeypatch.setenv("LEAD_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("DELIVERY_RETRY_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(LeadStorageError, match="^Lead storage unavailable$"):
        with TestClient(app):
            pass
    store.close.assert_awaited_once()
    assert "lead_storage_startup_failed stage=connection_schema_permissions error=RuntimeError sqlstate=28000" in caplog.text
    assert "private diagnostic" not in caplog.text


def test_pg_requires_retry_before_engine(managed_env, monkeypatch, fake_pg_modules):
    from fastapi.testclient import TestClient
    from app.main import app

    store, calls = fake_pg_modules
    monkeypatch.setenv("LEAD_STORAGE_BACKEND", "postgres")
    get_settings.cache_clear()
    with pytest.raises(LeadStorageError, match="requires delivery retries"):
        with TestClient(app):
            pass
    assert calls == []
    store.initialize.assert_not_awaited()


def test_backend_setting_defaults_to_files_and_rejects_unknown():
    assert Settings(_env_file=None).lead_storage_backend == "files"
    with pytest.raises(ValueError):
        Settings(_env_file=None, lead_storage_backend="unknown")


def test_snapshot_isolated_between_concurrent_tasks(tmp_path):
    store = Store()
    service = AnalyticsService(tmp_path / "events", tmp_path / "leads", postgres_lead_store=store)

    async def check():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first():
            async with service.leads_snapshot("dup_one"):
                entered.set()
                await release.wait()
                assert service._all_leads() == store.rows

        async def second():
            await entered.wait()
            async with service.leads_snapshot("dup_two"):
                assert service._all_leads() == []
                release.set()
                await asyncio.sleep(0)
                assert service._all_leads() == []

        await asyncio.gather(first(), second())

    asyncio.run(check())


def test_shutdown_stops_workers_even_if_one_failed():
    from app.storage.runtime import close_lead_storage

    async def check():
        stopped = []

        async def active():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        async def failed():
            raise RuntimeError("test worker failure")

        running, broken = asyncio.create_task(active()), asyncio.create_task(failed())
        await asyncio.sleep(0)
        closed_after_workers = []

        def closed():
            closed_after_workers.append(bool(stopped) and running.done() and broken.done())

        close = AsyncMock(side_effect=closed)
        app = SimpleNamespace(state=SimpleNamespace(
            postgres_lead_store=SimpleNamespace(close=close), background_tasks={"active": running, "failed": broken},
        ))
        await close_lead_storage(app)
        close.assert_awaited_once()
        assert closed_after_workers == [True]
        assert stopped and running.cancelled() and broken.done()

    asyncio.run(check())


def test_lead_database_credentials_do_not_fall_back_to_auth(monkeypatch):
    pytest.importorskip("sqlalchemy")
    from app.db.connection import database_url

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://auth_role@localhost/auth_db")
    for key in ("DATABASE_URL", "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv("LEAD_" + key, raising=False)
    with pytest.raises(ValueError):
        database_url("LEAD_")
    assert database_url().username == "auth_role"
    monkeypatch.setenv("LEAD_DATABASE_URL", "postgresql+asyncpg://lead_role@localhost/lead_db")
    assert database_url("LEAD_").username == "lead_role"


def test_lead_database_components_and_engine_prefix(monkeypatch):
    pytest.importorskip("sqlalchemy")
    from app.db import connection

    monkeypatch.delenv("LEAD_DATABASE_URL", raising=False)
    for key, value in {"PGHOST": "localhost", "PGUSER": "lead_role", "PGPASSWORD": "test-only",
                       "PGDATABASE": "lead_db", "PGPORT": "5433"}.items():
        monkeypatch.setenv("LEAD_" + key, value)
    url = connection.database_url("LEAD_")
    assert (url.username, url.database, url.port) == ("lead_role", "lead_db", 5433)
    monkeypatch.setattr(connection, "create_async_engine", lambda url, **kwargs: url)
    assert connection.make_engine("LEAD_") == url


def test_watchdog_digest_reads_sql_snapshot(test_client, monkeypatch):
    from app import watchdog as module

    store = attach_store(test_client, monkeypatch)
    settings = test_client.app.state.settings
    monkeypatch.setattr(settings, "watchdog_first_delay_seconds", 0)
    monkeypatch.setattr(settings, "watchdog_interval_seconds", 0)
    watchdog = SimpleNamespace(started=AsyncMock(), evaluate=AsyncMock(), problems=lambda: [],
        digest_due=lambda **kwargs: True, send_digest=AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(module, "Watchdog", lambda **kwargs: watchdog)
    monkeypatch.setattr(module, "ping_llm_if_idle", AsyncMock())
    monkeypatch.setattr(module, "run_preflight", AsyncMock(return_value={"checks": {}}))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(module.run_watchdog_loop(test_client.app), timeout=1))
    assert store.calls == [None]
    watchdog.send_digest.assert_awaited_once()
