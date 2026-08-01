"""проверки Telegram Topics бриджа (long polling, claim-кнопка, живая пересылка)."""

from __future__ import annotations

from typing import Any

import anyio

from app import telegram_bridge as telegram_bridge_module
from app.models import MessageRole
from app.sessions import SessionStore
from app.telegram_bridge import TelegramBridgeService


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    calls: list[dict[str, Any]] = []
    responses: dict[str, dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        method = url.rsplit("/", 1)[-1]
        FakeAsyncClient.calls.append({"method": method, "json": json})
        payload = FakeAsyncClient.responses.get(method, {"ok": True, "result": {}})
        return FakeResponse(payload)


class FakeWsManager:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_to_client(self, session_id: str, payload: dict[str, Any]) -> None:
        self.sent.append((session_id, payload))


def _reset_fake_client(monkeypatch) -> None:
    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = {}
    monkeypatch.setattr(telegram_bridge_module.httpx, "AsyncClient", FakeAsyncClient)


def _service(store: SessionStore, ws_manager: FakeWsManager, *, group_chat_id: str = "-100123") -> TelegramBridgeService:
    return TelegramBridgeService(
        bot_token="token",
        group_chat_id=group_chat_id,
        session_store=store,
        ws_manager=ws_manager,
    )


def test_disabled_without_group_id(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store, FakeWsManager(), group_chat_id="")

    assert service.enabled is False
    anyio.run(service.run_polling_loop)
    assert FakeAsyncClient.calls == []


def test_ensure_topic_for_session_creates_topic_with_claim_button(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> int | None:
        session = await store.get_or_create(None, "rosh_demo")
        FakeAsyncClient.responses["createForumTopic"] = {
            "ok": True,
            "result": {"message_thread_id": 42},
        }
        service = _service(store, ws_manager)
        topic_id = await service.ensure_topic_for_session(
            session_id=session.session_id,
            topic_name="Иван",
            card_text="карточка",
        )
        refreshed = await store.get(session.session_id)
        assert refreshed.telegram_topic_id == 42
        return topic_id

    topic_id = anyio.run(run)

    assert topic_id == 42
    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(send_calls) == 1
    keyboard = send_calls[0]["json"]["reply_markup"]["inline_keyboard"][0][0]
    assert keyboard["text"] == "Взять в работу"
    assert keyboard["callback_data"].startswith("claim:")


def test_ensure_topic_for_session_is_idempotent(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> int | None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=7)
        service = _service(store, ws_manager)
        return await service.ensure_topic_for_session(
            session_id=session.session_id,
            topic_name="Иван",
            card_text="карточка",
        )

    topic_id = anyio.run(run)

    assert topic_id == 7
    assert FakeAsyncClient.calls == []


def test_forward_client_message_sends_to_existing_topic(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)
        await service.forward_client_message(session.session_id, "хочу записаться")

    anyio.run(run)

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["message_thread_id"] == 42
    assert "хочу записаться" in send_calls[0]["json"]["text"]


def test_forward_client_message_noop_without_topic(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        service = _service(store, ws_manager)
        await service.forward_client_message(session.session_id, "хочу записаться")

    anyio.run(run)

    assert FakeAsyncClient.calls == []


def test_claim_locks_topic_to_first_operator(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> tuple[str | None, str | None]:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)

        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )
        first_claim = (await store.get(session.session_id)).telegram_claimed_by

        FakeAsyncClient.calls.clear()
        await service._handle_callback_query(
            {
                "id": "cb2",
                "data": f"claim:{session.session_id}",
                "from": {"username": "petya"},
                "message": {"message_id": 100},
            }
        )
        second_claim = (await store.get(session.session_id)).telegram_claimed_by
        return first_claim, second_claim

    first_claim, second_claim = anyio.run(run)

    assert first_claim == "masha"
    assert second_claim == "masha"  # второй клик не перезаписывает claim
    # второй клик не должен снова переименовывать тему
    rename_calls = [call for call in FakeAsyncClient.calls if call["method"] == "editForumTopic"]
    assert rename_calls == []


def test_handle_message_routes_operator_reply_to_correct_session(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)
        await service._handle_message({"message_thread_id": 42, "text": "Добрый день!"})
        return session.session_id

    session_id = anyio.run(run)

    async def check() -> None:
        refreshed = await store.get(session_id)
        assert refreshed.messages[-1].role == MessageRole.OPERATOR
        assert refreshed.messages[-1].text == "Добрый день!"

    anyio.run(check)
    assert ws_manager.sent == [
        (session_id, {"type": "message", "role": "operator", "text": "Добрый день!", "session_id": session_id})
    ]


def test_handle_message_ignores_unknown_topic(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager)
        await service._handle_message({"message_thread_id": 999, "text": "hello"})

    anyio.run(run)

    assert ws_manager.sent == []
