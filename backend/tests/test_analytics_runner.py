"""Bounded background reports without a database or external services."""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.analytics import AnalyticsService
from app.services.analytics_runner import AnalyticsRunner
from app.storage.errors import LeadStorageError
from app.utils.jsonl import append_jsonl


async def entered(event):
    assert await asyncio.to_thread(event.wait, 3), "worker did not start"


def test_distinct_jobs_share_one_thread_and_copy_context():
    async def check():
        runner = AnalyticsRunner()
        value = ContextVar("request_value")
        started, release = threading.Event(), threading.Event()
        active = peak = 0
        threads = set()

        def build():
            nonlocal active, peak
            threads.add(threading.get_ident())
            active += 1
            peak = max(peak, active)
            started.set()
            assert release.wait(3)
            time.sleep(0.005)
            active -= 1
            return value.get()

        async def request(index):
            value.set(index)
            return await runner.run(("dashboard", f"company-{index}", index), build)

        jobs = [asyncio.create_task(request(index)) for index in range(9)]
        try:
            await entered(started)
            assert active == 1
            release.set()
            assert await asyncio.gather(*jobs) == list(range(9))
            assert peak == 1 and len(threads) == 1
            assert threading.get_ident() not in threads
        finally:
            release.set()
            await runner.close()
        assert not runner._tasks

    asyncio.run(check())


def test_singleflight_survives_cancelled_joiner_and_does_not_cache():
    async def check():
        runner = AnalyticsRunner()
        started, release = threading.Event(), threading.Event()
        calls = []

        def build():
            calls.append(1)
            started.set()
            assert release.wait(3)
            return len(calls)

        first = asyncio.create_task(runner.run(("summary", "a"), build))
        try:
            await entered(started)
            second = asyncio.create_task(runner.run(("summary", "a"), build))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert len(calls) == 1
            release.set()
            assert await second == 1
            assert await runner.run(("summary", "a"), build) == 2
        finally:
            release.set()
            await runner.close()

    asyncio.run(check())


def test_capacity_is_checked_before_snapshot_and_duplicates_can_join():
    async def check():
        runner = AnalyticsRunner(max_pending=1)
        started, release = asyncio.Event(), asyncio.Event()
        prepared = []

        @asynccontextmanager
        async def snapshot():
            prepared.append(1)
            started.set()
            await release.wait()
            yield

        first = asyncio.create_task(runner.run(("summary", "a"), lambda: "a", prepare=snapshot))
        await started.wait()
        queued = asyncio.create_task(runner.run(("summary", "b"), lambda: "b", prepare=snapshot))
        duplicate = asyncio.create_task(runner.run(("summary", "a"), lambda: "wrong", prepare=snapshot))
        await asyncio.sleep(0)
        try:
            with pytest.raises(HTTPException) as error:
                await runner.run(("summary", "c"), lambda: "c", prepare=snapshot)
            assert error.value.status_code == 503
            assert error.value.detail == "Analytics busy"
            assert prepared == [1]
            release.set()
            assert await asyncio.gather(first, queued, duplicate) == ["a", "b", "a"]
            assert prepared == [1, 1]
        finally:
            release.set()
            await runner.close()

    asyncio.run(check())


def test_shutdown_cancels_queue_waits_for_active_thread_and_keeps_loop_responsive():
    async def check():
        runner = AnalyticsRunner()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        queued_calls = []

        def build():
            started.set()
            assert release.wait(3)
            finished.set()
            return 7

        job = asyncio.create_task(runner.run("first", build))
        await entered(started)
        queued = asyncio.create_task(runner.run("queued", lambda: queued_calls.append(1)))
        await asyncio.sleep(0)
        closing = asyncio.create_task(runner.close())
        await asyncio.sleep(0)
        try:
            assert not closing.done()
            with pytest.raises(HTTPException):
                await runner.run("new", lambda: 9)
            await asyncio.sleep(0.02)
            assert not closing.done() and not finished.is_set()
            release.set()
            await closing
            results = await asyncio.gather(job, queued, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            assert finished.is_set() and not queued_calls
            assert not runner._tasks
            assert all(not thread.is_alive() for thread in runner._executor._threads)
        finally:
            release.set()
            await closing

    asyncio.run(check())


def test_failure_frees_key_and_snapshot_context_reaches_dedicated_executor(tmp_path):
    async def check():
        rows = [{"company_id": "a", "timestamp": "2026-01-01", "reason": "booking_request"}]
        metadata = AsyncMock(return_value=rows)
        store = SimpleNamespace(reports=SimpleNamespace(lead_metadata=metadata))
        service = AnalyticsService(tmp_path / "events", tmp_path / "leads", postgres_lead_store=store)
        try:
            with pytest.raises(ValueError):
                await service.runner.run("broken", lambda: (_ for _ in ()).throw(ValueError()))
            assert await service.runner.run("broken", lambda: "retry") == "retry"
            result = await service.runner.report(
                service, ("summary", "a"), service._all_leads, company_id="a", leads=True,
            )
            assert result == rows
            metadata.assert_awaited_once_with(company_id="a")
            with pytest.raises(LeadStorageError):
                service._all_leads()
        finally:
            await service.runner.close()

    asyncio.run(check())


def test_shutdown_unwinds_async_snapshot_before_returning():
    async def check():
        runner = AnalyticsRunner()
        opened = asyncio.Event()
        cleaned = []
        builds = []

        @asynccontextmanager
        async def snapshot():
            try:
                opened.set()
                await asyncio.Event().wait()
                yield
            finally:
                cleaned.append(True)

        job = asyncio.create_task(runner.run("reading", lambda: builds.append(1), prepare=snapshot))
        await opened.wait()
        await runner.close()
        assert cleaned == [True] and not builds
        assert isinstance((await asyncio.gather(job, return_exceptions=True))[0], asyncio.CancelledError)

    asyncio.run(check())


def test_route_keys_include_company_ranges_pagination_and_export_selection(test_client, monkeypatch):
    runner = test_client.app.state.analytics_service.runner
    original = runner.report
    keys = []

    async def capture(service, key, build, **kwargs):
        keys.append(key)
        return await original(service, key, build, **kwargs)

    monkeypatch.setattr(runner, "report", capture)
    headers = {"x-operator-token": "demo-operator-token"}
    queries = [
        "summary?company_id=one&limit=1", "summary?company_id=two&limit=1", "summary?company_id=one&limit=2",
        "operators?company_id=one", "operators?company_id=two",
        "dashboard?company_id=one&days=7", "dashboard?company_id=two&days=7", "dashboard?company_id=one&days=30",
        "dashboard?company_id=one&start_date=2026-01-01&end_date=2026-01-02",
        "dashboard?company_id=one&start_date=2026-01-01&end_date=2026-01-03",
        "chats?company_id=one", "chats?company_id=two", "chats?company_id=one&scope=operator",
        "chats?company_id=one&days=7", "chats?company_id=one&offset=1", "chats?company_id=one&limit=1",
        "chats?company_id=one&start_date=2026-01-01&end_date=2026-01-02",
        "leads?company_id=one", "leads?company_id=two", "leads?company_id=one&reason=booking_change",
        "leads?company_id=one&days=7", "leads?company_id=one&limit=1",
    ]
    for query in queries:
        response = test_client.get("/api/analytics/" + query, headers=headers)
        assert response.status_code == 200, response.text
    for payload in (
        {"company_id": "one"}, {"company_id": "two"}, {"company_id": "one", "scope": "lead"},
        {"company_id": "one", "session_ids": ["a", "b"]},
        {"company_id": "one", "session_ids": ["b", "a"]}, {"company_id": "one", "limit": 1},
    ):
        assert test_client.post("/api/analytics/chats/export", json=payload, headers=headers).status_code == 200
    assert len(keys) == len(queries) + 6
    assert len(set(keys)) == len(keys)


def test_scoped_export_and_detail_routes_never_read_leads(test_client, monkeypatch):
    from app.auth import CLIENT_COOKIE_NAME, Principal

    service = test_client.app.state.analytics_service
    for company in ("one", "two"):
        append_jsonl(service.conversations_archive_file, {
            "session_id": company, "company_id": company, "status": "CLOSED",
            "closed_at": "2026-11-10T00:00:00", "messages": [
                {"role": "user", "text": company, "created_at": "2026-11-10T00:00:00"},
            ],
        })
    monkeypatch.setattr(service, "postgres_lead_store", SimpleNamespace())
    monkeypatch.setattr(test_client.app.state.settings, "client_login_enabled", True)
    access = SimpleNamespace(authenticate=AsyncMock(return_value=Principal(owner=False, company_id="one")),
                             close=AsyncMock())
    monkeypatch.setattr(test_client.app.state, "client_access", access)
    assert test_client.post("/api/analytics/chats/export", json={}).status_code == 403
    test_client.cookies.set(CLIENT_COOKIE_NAME, "session")
    headers = {"origin": "http://testserver"}
    assert test_client.post("/api/analytics/chats/export", json={"company_id": "two"}, headers=headers).status_code == 403
    result = test_client.post("/api/analytics/chats/export", json={"session_ids": ["two", "one"]}, headers=headers)
    assert result.status_code == 200, result.text
    assert [item["session_id"] for item in result.json()["conversations"]] == ["one"]
    assert test_client.get("/api/analytics/chats/two").status_code == 404
    assert test_client.get("/api/analytics/chats/one").status_code == 200
    owner = {"x-operator-token": "demo-operator-token"}
    assert test_client.get("/api/analytics/chats/two", headers=owner).status_code == 200
    assert test_client.get("/api/analytics/chats/two").status_code == 404


def test_outer_lifespan_closes_runner_before_storage_even_on_shutdown_error(monkeypatch):
    from app import main

    async def check():
        calls = []
        runner = SimpleNamespace(close=AsyncMock(side_effect=lambda: calls.append("runner")))

        @asynccontextmanager
        async def failed_lifespan(app):
            app.state.analytics_service = SimpleNamespace(runner=runner)
            yield
            raise RuntimeError("failed background worker")

        monkeypatch.setattr(main, "_lifespan", failed_lifespan)
        monkeypatch.setattr(main, "close_lead_storage", AsyncMock(side_effect=lambda app: calls.append("storage")))
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(RuntimeError, match="failed background worker"):
            async with main.lifespan(app):
                pass
        assert calls == ["runner", "storage"]

    asyncio.run(check())
