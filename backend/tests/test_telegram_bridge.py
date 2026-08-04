"""проверки Telegram Topics бриджа (long polling, claim-кнопка, живая пересылка)."""

from __future__ import annotations

from typing import Any

import anyio

from app import telegram_bridge as telegram_bridge_module
from app.models import MessageRole
from app.sessions import SessionStore
from app.telegram_bridge import TelegramBridgeService, client_label_for_session, operator_label


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
        self.disconnect_operator_calls: list[dict[str, Any]] = []

    async def send_to_client(self, session_id: str, payload: dict[str, Any]) -> None:
        self.sent.append((session_id, payload))

    async def disconnect_operator(self, session_id: str, close_session: bool = True) -> None:
        self.disconnect_operator_calls.append({"session_id": session_id, "close_session": close_session})


def _reset_fake_client(monkeypatch) -> None:
    FakeAsyncClient.calls = []
    FakeAsyncClient.responses = {}
    monkeypatch.setattr(telegram_bridge_module.httpx, "AsyncClient", FakeAsyncClient)


def _service(
    store: SessionStore,
    ws_manager: FakeWsManager,
    *,
    group_chat_id: str = "-100123",
    clients_topic_id: str = "",
) -> TelegramBridgeService:
    return TelegramBridgeService(
        bot_token="token",
        group_chat_id=group_chat_id,
        session_store=store,
        ws_manager=ws_manager,
        clients_topic_id=clients_topic_id,
    )


def test_disabled_without_group_id(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store, FakeWsManager(), group_chat_id="")

    assert service.enabled is False
    anyio.run(service.run_polling_loop)
    assert FakeAsyncClient.calls == []


def test_post_operator_queue_card_sends_claim_button_to_general(monkeypatch) -> None:
    """General — очередь входящих, ждущих оператора. Карточка идёт БЕЗ message_thread_id
    (General — не отдельная тема), тема сессии тут ещё не создаётся."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager)
        await service.post_operator_queue_card(
            session_id="sess-1",
            reason="⚡️ Запросил оператора",
            last_message="хочу к менеджеру",
            client_label="Иван",
        )

    anyio.run(run)

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(send_calls) == 1
    assert "message_thread_id" not in send_calls[0]["json"]
    keyboard = send_calls[0]["json"]["reply_markup"]["inline_keyboard"][0][0]
    assert keyboard["text"] == "Взять в работу"
    assert keyboard["callback_data"] == "claim:sess-1"
    assert "Иван" in send_calls[0]["json"]["text"]
    create_calls = [call for call in FakeAsyncClient.calls if call["method"] == "createForumTopic"]
    assert create_calls == []


def test_post_client_lead_card_sends_to_clients_topic_without_button(monkeypatch) -> None:
    """Клиенты — простой лог лидов/записей без прямой необходимости в операторе. Без кнопки,
    без своей темы — контакт уже зафиксирован, никто не ждёт прямо сейчас."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager, clients_topic_id="99")
        await service.post_client_lead_card("🔔 Новый лид\n\nИмя: Мария\nТелефон: +7916...")

    anyio.run(run)

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["message_thread_id"] == 99
    assert "reply_markup" not in send_calls[0]["json"]


def test_post_client_lead_card_noop_without_clients_topic_configured(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager, clients_topic_id="")
        await service.post_client_lead_card("🔔 Новый лид")

    anyio.run(run)

    assert FakeAsyncClient.calls == []


def test_claim_creates_topic_named_after_client_and_operator_with_transcript(monkeypatch) -> None:
    """Тема создаётся ТОЛЬКО в момент клейма (не раньше — иначе плодим темы на заявки,
    которые никто не забрал), и сразу называется и клиентом, и оператором — не нужно
    отдельно переименовывать после."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.update_contact_draft(session.session_id, name="Мария Петровна")
        await store.append_message(session.session_id, MessageRole.USER, "хочу оставить телефон")
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager)

        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )

    anyio.run(run)

    create_calls = [call for call in FakeAsyncClient.calls if call["method"] == "createForumTopic"]
    assert len(create_calls) == 1
    assert create_calls[0]["json"]["name"] == "Мария Петровна · masha"

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert any("хочу оставить телефон" in call["json"]["text"] for call in send_calls)
    assert any("Взято в работу" in call["json"]["text"] for call in send_calls)


def test_claim_falls_back_to_session_id_when_no_contact_known(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager)
        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )
        return session.session_id

    session_id = anyio.run(run)

    create_calls = [call for call in FakeAsyncClient.calls if call["method"] == "createForumTopic"]
    assert create_calls[0]["json"]["name"] == f"Сессия {session_id[:8]} · masha"


def test_claim_reuses_existing_topic_without_recreating(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=7)
        service = _service(store, ws_manager)
        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )

    anyio.run(run)

    assert [c for c in FakeAsyncClient.calls if c["method"] == "createForumTopic"] == []


def test_claim_adds_jump_to_topic_link_on_general_card(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager, group_chat_id="-1001234567890")
        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )

    anyio.run(run)

    edit_calls = [c for c in FakeAsyncClient.calls if c["method"] == "editMessageReplyMarkup"]
    assert len(edit_calls) == 1
    buttons = edit_calls[0]["json"]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["text"] == "Взято: masha"
    assert buttons[1][0]["url"] == "https://t.me/c/1234567890/42"


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


def test_close_command_closes_session_and_topic_without_relaying_as_chat_message(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)
        await service._handle_message({"message_thread_id": 42, "text": "/done"})
        return session.session_id

    session_id = anyio.run(run)

    assert ws_manager.disconnect_operator_calls == [{"session_id": session_id, "close_session": True}]
    # команда закрытия не должна попадать в чат клиента как обычное сообщение оператора
    assert ws_manager.sent == []

    close_calls = [call for call in FakeAsyncClient.calls if call["method"] == "closeForumTopic"]
    assert len(close_calls) == 1
    assert close_calls[0]["json"]["message_thread_id"] == 42

    confirm_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(confirm_calls) == 1
    assert "завершён" in confirm_calls[0]["json"]["text"]

    async def check_session() -> None:
        refreshed = await store.get(session_id)
        # append_message для команды закрытия быть не должно
        assert refreshed.messages == []

    anyio.run(check_session)


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


def test_operator_label_prefers_short_first_name_over_long_username() -> None:
    """Живой баг: тема называлась 'Сессия 80b3b84d · sKiTTlesSkiiiiiirRrtEssskeeeetit' —
    ник Telegram может быть произвольно длинной строкой, из-за неё название обрезалось в
    сайдбаре и терялось целиком. first_name почти всегда короткое настоящее имя."""

    label = operator_label({"username": "sKiTTlesSkiiiiiirRrtEssskeeeetit", "first_name": "Алексей"})

    assert label == "Алексей"


def test_operator_label_falls_back_to_username_when_no_first_name() -> None:
    assert operator_label({"username": "masha"}) == "masha"


def test_operator_label_caps_length_even_for_short_field_names() -> None:
    label = operator_label({"first_name": "оченьдлинноеимяоператорачтобытосамое"})

    assert len(label) <= 18


def test_client_label_for_session_caps_long_names(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.update_contact_draft(session.session_id, name="Оченьдлинноеимяклиентадлятестаточно")
        refreshed = await store.get(session.session_id)
        return client_label_for_session(refreshed)

    label = anyio.run(run)

    assert len(label) <= 18


def test_claim_topic_name_stays_compact_with_long_username(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager)
        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "sKiTTlesSkiiiiiirRrtEssskeeeetit"},
                "message": {"message_id": 100},
            }
        )

    anyio.run(run)

    create_calls = [call for call in FakeAsyncClient.calls if call["method"] == "createForumTopic"]
    assert len(create_calls[0]["json"]["name"]) <= 45
