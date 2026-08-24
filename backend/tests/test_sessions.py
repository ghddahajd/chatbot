"""проверки предохранителей SessionStore."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

import anyio

from app.models import SessionStatus
from app.sessions import SessionStore


def test_evict_stale_removes_closed_and_ai_active_but_keeps_recent_operator_sessions() -> None:
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

        # 2 дня старее обычного 24ч ttl, но моложе страховочного operator_ttl_seconds (тут 5 дней) —
        # оператор ещё может держать диалог у себя, эвиктить рано.
        removed = await store.evict_stale(ttl_seconds=86400, operator_ttl_seconds=86400 * 5)

        assert removed == 2
        assert await store.get(ai_session.session_id) is None
        assert await store.get(closed_session.session_id) is None
        assert await store.get(waiting_session.session_id) is not None
        assert await store.get(human_session.session_id) is not None

    anyio.run(run)


def test_evict_stale_without_operator_ttl_never_touches_operator_sessions() -> None:
    async def run() -> None:
        store = SessionStore()
        waiting_session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        waiting_session.updated_at = datetime.utcnow() - timedelta(days=365)

        removed = await store.evict_stale(ttl_seconds=86400)

        assert removed == 0
        assert await store.get(waiting_session.session_id) is not None

    anyio.run(run)


def test_evict_stale_eventually_removes_abandoned_operator_sessions() -> None:
    async def run() -> None:
        store = SessionStore()
        waiting_session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        waiting_session.updated_at = datetime.utcnow() - timedelta(days=3)

        # 3 дня старше страховочного operator_ttl_seconds (2 дня) — считаем диалог заброшенным.
        removed = await store.evict_stale(ttl_seconds=86400, operator_ttl_seconds=172800)

        assert removed == 1
        assert await store.get(waiting_session.session_id) is None

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


def test_snapshot_to_keeps_stale_operator_session_under_the_longer_operator_ttl(tmp_path: Path) -> None:
    snapshot_file = tmp_path / "sessions.json"

    async def run() -> None:
        store = SessionStore()
        waiting_session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        # Старее обычного 24ч session_ttl_seconds, но моложе 2-дневного operator_ttl_seconds —
        # раньше это же условие роняло сессию из снапшота при рестарте/деплое (баг с "Сессия
        # не найдена" в Telegram, когда карточка в очереди остаётся, а сессии за ней уже нет).
        waiting_session.updated_at = datetime.utcnow() - timedelta(hours=30)

        count = await store.snapshot_to(snapshot_file, ttl_seconds=86400, operator_ttl_seconds=172800)
        restored_store = SessionStore()
        restored_count = await restored_store.restore_from(snapshot_file)

        assert count == 1
        assert restored_count == 1
        assert await restored_store.get(waiting_session.session_id) is not None

    anyio.run(run)


def test_snapshot_to_drops_operator_session_past_the_operator_ttl(tmp_path: Path) -> None:
    snapshot_file = tmp_path / "sessions.json"

    async def run() -> None:
        store = SessionStore()
        waiting_session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(waiting_session.session_id, SessionStatus.WAITING_OPERATOR)
        waiting_session.updated_at = datetime.utcnow() - timedelta(days=3)

        count = await store.snapshot_to(snapshot_file, ttl_seconds=86400, operator_ttl_seconds=172800)

        assert count == 0

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


def test_lock_for_returns_same_lock_object_for_same_session_id() -> None:
    store = SessionStore()

    assert store.lock_for("abc") is store.lock_for("abc")
    assert store.lock_for("abc") is not store.lock_for("xyz")


def test_lock_for_serializes_concurrent_read_modify_write_for_same_session() -> None:
    """Доказывает, зачем нужен per-session лок: без сериализации конкурентные
    read-increment-write по одному session_id теряют часть инкрементов (classic lost
    update) — ровно то, что раньше могло происходить между отдельными locked-операциями
    SessionStore внутри одного handle_message (get_or_create -> ... -> update_context)."""

    store = SessionStore()
    counter = {"value": 0}

    async def increment_under_lock(session_id: str) -> None:
        async with store.lock_for(session_id):
            current = counter["value"]
            await asyncio.sleep(0.01)  # имитирует I/O между чтением и записью
            counter["value"] = current + 1

    async def run() -> None:
        await asyncio.gather(*(increment_under_lock("s1") for _ in range(10)))

    anyio.run(run)
    assert counter["value"] == 10


def test_lock_for_does_not_serialize_across_different_sessions() -> None:
    """Лок должен быть per-session, а не общий на весь SessionStore — иначе параллельная
    обработка НЕСВЯЗАННЫХ сессий превратилась бы в узкое горлышко."""

    store = SessionStore()

    async def hold_lock(session_id: str, delay: float) -> None:
        async with store.lock_for(session_id):
            await asyncio.sleep(delay)

    async def run() -> float:
        started = time.perf_counter()
        await asyncio.gather(hold_lock("a", 0.08), hold_lock("b", 0.08))
        return time.perf_counter() - started

    elapsed = anyio.run(run)
    # последовательно ушло бы ~0.16s; параллельно должно быть заметно меньше
    assert elapsed < 0.14


def test_evict_stale_cleans_up_session_lock() -> None:
    async def run() -> None:
        store = SessionStore()
        session = await store.get_or_create(None, "rosh_demo")
        lock_before = store.lock_for(session.session_id)
        session.updated_at = datetime.utcnow() - timedelta(days=2)

        await store.evict_stale(ttl_seconds=3600)

        # session_locks — defaultdict, поэтому просто заново создаёт лок при следующем
        # обращении; проверяем, что это НОВЫЙ объект, а не тот же самый (значит старый
        # был реально удалён, а не просто переиспользован).
        lock_after = store.lock_for(session.session_id)
        assert lock_before is not lock_after

    anyio.run(run)
