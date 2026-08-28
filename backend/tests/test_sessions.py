"""проверки предохранителей SessionStore."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import anyio

from app.models import Message, MessageRole, Session, SessionStatus
from app.sessions import SessionStore, archive_session


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

        assert len(removed) == 2
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

        assert removed == []
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

        assert len(removed) == 1
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

        result = await store.snapshot_to(snapshot_file, ttl_seconds=86400)
        restored_store = SessionStore()
        restored_count = await restored_store.restore_from(snapshot_file)

        assert result.kept_count == 2
        assert restored_count == 2
        assert await restored_store.get(active_session.session_id) is not None
        assert await restored_store.get(waiting_session.session_id) is not None
        assert await restored_store.get(closed_session.session_id) is None
        # Живой баг (ручное тестирование пользователем, 2026-08-29): closed_session
        # выброшена из снапшота, но должна быть возвращена вызывающей стороне для архивации —
        # иначе она пропадает насовсем, минуя archive_session, при рестарте сразу после закрытия.
        assert [s.session_id for s in result.dropped] == [closed_session.session_id]

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

        result = await store.snapshot_to(snapshot_file, ttl_seconds=86400, operator_ttl_seconds=172800)
        restored_store = SessionStore()
        restored_count = await restored_store.restore_from(snapshot_file)

        assert result.kept_count == 1
        assert result.dropped == []
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

        result = await store.snapshot_to(snapshot_file, ttl_seconds=86400, operator_ttl_seconds=172800)

        assert result.kept_count == 0
        # Живой баг (ручное тестирование пользователем, 2026-08-29): раньше эта заброшенная
        # операторская сессия просто пропадала бы из снапшота без следа — теперь вызывающая
        # сторона узнаёт о ней и может архивировать, а не потерять молча.
        assert [s.session_id for s in result.dropped] == [waiting_session.session_id]

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


def test_archive_session_writes_full_conversation_with_all_roles(tmp_path: Path) -> None:
    """2026-08-29: живая часть эскалированных к оператору диалогов раньше не архивировалась
    нигде, кроме памяти (пропадает вместе с TTL) и самого Telegram — archive_session закрывает
    именно этот пробел, пишет ВСЕ роли (клиент/бот/оператор) одним диалогом."""

    archive_file = tmp_path / "conversations_archive.jsonl"
    session = Session(
        session_id="archive-test-1",
        company_id="rosh_demo",
        status=SessionStatus.CLOSED,
        messages=[
            Message(role=MessageRole.USER, text="хочу оператора"),
            Message(role=MessageRole.ASSISTANT, text="Соединяю с менеджером", kind="handoff"),
            Message(role=MessageRole.OPERATOR, text="Здравствуйте, чем помочь?"),
        ],
        lead_requested=True,
        operator_requested=True,
        telegram_claimed_by="masha",
    )

    archive_session(archive_file, session)

    lines = archive_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "archive-test-1"
    assert record["company_id"] == "rosh_demo"
    assert record["status"] == "CLOSED"
    assert record["lead_requested"] is True
    assert record["telegram_claimed_by"] == "masha"
    assert [m["role"] for m in record["messages"]] == ["user", "assistant", "operator"]
    assert record["messages"][2]["text"] == "Здравствуйте, чем помочь?"
    assert record["messages"][1]["kind"] == "handoff"


def test_archive_session_appends_multiple_sessions(tmp_path: Path) -> None:
    archive_file = tmp_path / "conversations_archive.jsonl"
    first = Session(session_id="s1", company_id="rosh_demo", messages=[Message(role=MessageRole.USER, text="a")])
    second = Session(session_id="s2", company_id="rosh_demo", messages=[Message(role=MessageRole.USER, text="b")])

    archive_session(archive_file, first)
    archive_session(archive_file, second)

    lines = archive_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["session_id"] == "s1"
    assert json.loads(lines[1])["session_id"] == "s2"


def test_snapshot_then_archive_dropped_captures_a_freshly_closed_session(tmp_path: Path) -> None:
    """Живой баг (ручное тестирование пользователем, 2026-08-29): оператор закрыл диалог, и
    почти сразу (до следующего тика evict_stale) произошёл рестарт/деплой — снапшот на
    выключении молча выбрасывал CLOSED-сессии (см. _keep в snapshot_to), archive_session для
    неё не вызывался вообще, переписка терялась насовсем. Проверяем полный путь: снапшот
    возвращает её в dropped, archive_session на dropped сохраняет полный текст."""

    snapshot_file = tmp_path / "sessions.json"
    archive_file = tmp_path / "conversations_archive.jsonl"

    async def run() -> None:
        store = SessionStore()
        session = await store.get_or_create(None, "rosh_demo")
        await store.append_message(session.session_id, MessageRole.USER, "хочу оператора")
        await store.append_message(session.session_id, MessageRole.OPERATOR, "Здравствуйте!")
        # Закрыт только что — свежее любого TTL, поэтому evict_stale его бы НЕ тронул.
        await store.set_status(session.session_id, SessionStatus.CLOSED)

        result = await store.snapshot_to(snapshot_file, ttl_seconds=86400, operator_ttl_seconds=172800)
        for dropped_session in result.dropped:
            archive_session(archive_file, dropped_session)
        return session.session_id

    session_id = anyio.run(run)

    lines = archive_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == session_id
    assert record["status"] == "CLOSED"
    assert [m["text"] for m in record["messages"]] == ["хочу оператора", "Здравствуйте!"]


def test_snapshot_to_removes_dropped_sessions_to_avoid_duplicate_archiving(tmp_path: Path) -> None:
    """Живой баг (ручное тестирование пользователем, 2026-08-29): CLOSED-сессия, попавшая в
    dropped, оставалась в памяти — на каждом следующем snapshot_to (например, при коротком
    интервале эвикции + рестарте почти сразу после) она снова оказывалась в dropped и
    архивировалась бы повторно, дублируя запись. Теперь она реально удаляется из store сразу."""

    snapshot_file = tmp_path / "sessions.json"

    async def run() -> tuple[str, int]:
        store = SessionStore()
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.CLOSED)

        first = await store.snapshot_to(snapshot_file, ttl_seconds=86400)
        second = await store.snapshot_to(snapshot_file, ttl_seconds=86400)
        return session.session_id, len(first.dropped), len(second.dropped)

    session_id, first_dropped_count, second_dropped_count = anyio.run(run)

    assert first_dropped_count == 1
    assert second_dropped_count == 0
