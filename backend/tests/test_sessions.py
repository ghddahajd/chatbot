"""проверки предохранителей SessionStore."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import anyio

from app.models import SessionStatus
from app.sessions import SessionStore


def test_evict_stale_removes_closed_and_ai_active_but_keeps_operator_sessions() -> None:
    async def run() -> None:
        store = SessionStore()
        ai_session = await store.get_or_create(None, "rosh_demo")
        closed_session = await store.get_or_create(None, "rosh_demo")
        waiting_session = await store.get_or_create(None, "rosh_demo")
        human_session = await store.get_or_create(None, "rosh_demo")

        await store.set_status(closed_session.session_id, SessionStatus.CLOSED)
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        await store.set_status(human_session.session_id, SessionStatus.HUMAN_ACTIVE)

        stale_time = datetime.utcnow() - timedelta(days=2)
        ai_session.updated_at = stale_time
        closed_session.updated_at = stale_time
        waiting_session.updated_at = stale_time
        human_session.updated_at = stale_time

        removed = await store.evict_stale(ttl_seconds=86400)

        assert removed == 2
        assert await store.get(ai_session.session_id) is None
        assert await store.get(closed_session.session_id) is None
        assert await store.get(waiting_session.session_id) is not None
        assert await store.get(human_session.session_id) is not None

    anyio.run(run)


def test_snapshot_restore_keeps_active_non_closed_sessions(tmp_path: Path) -> None:
    snapshot_file = tmp_path / "sessions.json"

    async def run() -> None:
        store = SessionStore()
        active_session = await store.get_or_create(None, "rosh_demo")
        waiting_session = await store.get_or_create(None, "rosh_demo")
        closed_session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        await store.set_status(closed_session.session_id, SessionStatus.CLOSED)

        count = await store.snapshot_to(snapshot_file, ttl_seconds=86400)
        restored_store = SessionStore()
        restored_count = await restored_store.restore_from(snapshot_file)

        assert count == 2
        assert restored_count == 2
        assert await restored_store.get(active_session.session_id) is not None
        assert await restored_store.get(waiting_session.session_id) is not None
        assert await restored_store.get(closed_session.session_id) is None

    anyio.run(run)


def test_restore_from_corrupt_snapshot_starts_empty(tmp_path: Path) -> None:
    snapshot_file = tmp_path / "sessions.json"
    snapshot_file.write_text("{not-json", encoding="utf-8")

    async def run() -> None:
        store = SessionStore()
        restored_count = await store.restore_from(snapshot_file)

        assert restored_count == 0
        assert await store.list_all() == []

    anyio.run(run)
