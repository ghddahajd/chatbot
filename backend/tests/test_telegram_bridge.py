"""проверки Telegram Topics бриджа (long polling, claim-кнопка, живая пересылка)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import anyio
import httpx

from app import telegram_bridge as telegram_bridge_module
from app.models import MessageRole, SessionStatus
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
    init_kwargs: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeAsyncClient.init_kwargs.append(kwargs)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        method = url.rsplit("/", 1)[-1]
        FakeAsyncClient.calls.append({"method": method, "json": json})
        configured = FakeAsyncClient.responses.get(method, {"ok": True, "result": {}})
        # список — последовательные ответы на повторные вызовы (для тестов ретрая),
        # словарь — как раньше, один и тот же ответ на каждый вызов.
        if isinstance(configured, list):
            payload = configured.pop(0) if len(configured) > 1 else configured[0]
        else:
            payload = configured
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
    FakeAsyncClient.init_kwargs = []
    monkeypatch.setattr(telegram_bridge_module.httpx, "AsyncClient", FakeAsyncClient)


def _service(
    store: SessionStore,
    ws_manager: FakeWsManager,
    *,
    group_chat_id: str = "-100123",
    clients_topic_id: str = "",
    failures_file: Path | None = None,
    proxy_url: str = "",
    analytics_service: Any = None,
) -> TelegramBridgeService:
    return TelegramBridgeService(
        bot_token="token",
        group_chat_id=group_chat_id,
        session_store=store,
        ws_manager=ws_manager,
        clients_topic_id=clients_topic_id,
        failures_file=failures_file,
        proxy_url=proxy_url,
        analytics_service=analytics_service,
    )


class FakeAnalyticsService:
    """Спай вместо реального AnalyticsService — фиксирует track_event-вызовы без записи на
    диск, чтобы проверить именно факт и содержимое operator_claimed/operator_closed событий."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def track_event(
        self,
        *,
        company_id: str,
        session_id: str,
        event_type: str,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "company_id": company_id,
                "session_id": session_id,
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
            }
        )


def test_disabled_without_group_id(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store, FakeWsManager(), group_chat_id="")

    assert service.enabled is False
    anyio.run(service.run_polling_loop)
    assert FakeAsyncClient.calls == []


def test_proxy_url_is_passed_to_httpx_client(monkeypatch) -> None:
    """Живой баг (2026-08-26): исходящий TCP по IPv6 с сервера не работает вообще, а по IPv4
    избирательно заблокирован диапазон адресов Telegram — единственный рабочий обход прямо
    сейчас — HTTP-прокси. Проверяем, что proxy_url реально доходит до httpx.AsyncClient, а
    не теряется по дороге."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(
        store, FakeWsManager(), proxy_url="http://user:pass@196.19.122.75:8000"
    )

    async def run() -> None:
        await service.post_operator_queue_card(
            session_id="sess-1",
            reason="⚡️ Запросил оператора",
            last_message="хочу к менеджеру",
            client_label="Иван",
        )

    anyio.run(run)

    assert FakeAsyncClient.init_kwargs
    assert FakeAsyncClient.init_kwargs[-1]["proxy"] == "http://user:pass@196.19.122.75:8000"


def test_no_proxy_by_default(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store, FakeWsManager())

    async def run() -> None:
        await service.post_operator_queue_card(
            session_id="sess-1",
            reason="⚡️ Запросил оператора",
            last_message="хочу к менеджеру",
            client_label="Иван",
        )

    anyio.run(run)

    assert FakeAsyncClient.init_kwargs
    assert FakeAsyncClient.init_kwargs[-1]["proxy"] is None


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


def test_call_retries_on_429_and_respects_retry_after(monkeypatch) -> None:
    """Живой баг, найден нагрузочным тестом: несколько параллельных карточек в одну и ту же
    группу операторов — Telegram лимитирует ~1 сообщение/сек в чат, отвечает 429, раньше это
    молча логировалось и карточка терялась без следа (18 из 57 при 10 параллельных запросах).
    Теперь должны уважать retry_after и повторить, а не потерять."""

    _reset_fake_client(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_bridge_module.asyncio, "sleep", fake_sleep)
    FakeAsyncClient.responses["sendMessage"] = [
        {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 2", "parameters": {"retry_after": 2}},
        {"ok": True, "result": {"message_id": 1}},
    ]

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
    assert len(send_calls) == 2
    assert sleep_calls == [2]


def test_call_gives_up_after_max_retries_on_persistent_429(monkeypatch) -> None:
    """Не должен ретраить бесконечно — если Telegram стабильно возвращает 429, в какой-то
    момент сдаётся и просто логирует, как раньше делал сразу."""

    _reset_fake_client(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_bridge_module.asyncio, "sleep", fake_sleep)
    FakeAsyncClient.responses["sendMessage"] = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 1",
        "parameters": {"retry_after": 1},
    }

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
    assert len(send_calls) == telegram_bridge_module._MAX_RATE_LIMIT_RETRIES + 1
    assert len(sleep_calls) == telegram_bridge_module._MAX_RATE_LIMIT_RETRIES


def test_call_retries_short_and_bounded_on_non_rate_limit_error(monkeypatch) -> None:
    """Живой баг (ручное тестирование пользователем, 2026-08-26): ошибка не про лимит (409
    Conflict от параллельного инстанса, 5xx, сетевой сбой) раньше не ретраилась вообще — одна
    неудачная попытка теряла карточку насовсем. Теперь короткий ограниченный повтор (несколько
    секунд, не фоновая очередь) — ловит переходные сбои, но не зависает навечно на постоянной
    ошибке вроде неверного chat_id: попыток строго _MAX_TRANSIENT_RETRIES+1, не больше."""

    _reset_fake_client(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_bridge_module.asyncio, "sleep", fake_sleep)
    FakeAsyncClient.responses["sendMessage"] = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: chat not found",
    }

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
    assert len(send_calls) == telegram_bridge_module._MAX_TRANSIENT_RETRIES + 1
    assert sleep_calls == [telegram_bridge_module._TRANSIENT_RETRY_DELAY_SECONDS] * telegram_bridge_module._MAX_TRANSIENT_RETRIES


def test_call_retries_and_recovers_on_conflict_error(monkeypatch) -> None:
    """Живой сценарий, который реально произошёл: 409 Conflict (два инстанса опрашивают
    getUpdates одновременно) на отправку карточки — если следующая попытка через пару секунд
    проходит успешно, карточка должна дойти, а не потеряться."""

    _reset_fake_client(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_bridge_module.asyncio, "sleep", fake_sleep)
    FakeAsyncClient.responses["sendMessage"] = [
        {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"},
        {"ok": True, "result": {"message_id": 1}},
    ]

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
    assert len(send_calls) == 2
    assert sleep_calls == [telegram_bridge_module._TRANSIENT_RETRY_DELAY_SECONDS]


def test_call_retries_on_network_exception(monkeypatch) -> None:
    """Сетевой сбой (ConnectError/Timeout) раньше не ловился вообще внутри _call — исключение
    улетало наверх и терялось в generic except Exception у вызывающего кода, без единой попытки
    повтора. Теперь тоже короткий ограниченный ретрай, как и для не-429 ошибок API."""

    _reset_fake_client(monkeypatch)
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_bridge_module.asyncio, "sleep", fake_sleep)

    call_count = 0
    real_post = FakeAsyncClient.post

    async def flaky_post(self, url: str, json: dict) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("boom")
        return await real_post(self, url, json)

    monkeypatch.setattr(FakeAsyncClient, "post", flaky_post)
    FakeAsyncClient.responses["sendMessage"] = {"ok": True, "result": {"message_id": 1}}

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

    assert call_count == 2
    assert sleep_calls == [telegram_bridge_module._TRANSIENT_RETRY_DELAY_SECONDS]


def test_failed_send_writes_a_durable_failure_record(monkeypatch, tmp_path) -> None:
    """Живая дыра (2026-08-20): неудачная отправка (не 429) не оставляла НИКАКОГО следа кроме
    одной строчки в логе — не было способа потом узнать, дошла ли конкретная карточка. Теперь
    неудача пишется в failures_file, с session_id для привязки к конкретному лиду."""

    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["sendMessage"] = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: chat not found",
    }
    failures_file = tmp_path / "telegram_bridge_failures.jsonl"
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager, failures_file=failures_file)
        await service.post_operator_queue_card(
            session_id="sess-1",
            reason="⚡️ Запросил оператора",
            last_message="хочу к менеджеру",
            client_label="Иван",
        )

    anyio.run(run)

    from app.utils.jsonl import read_jsonl

    records = read_jsonl(failures_file)
    assert len(records) == 1
    assert records[0]["kind"] == "operator_queue_card"
    assert records[0]["session_id"] == "sess-1"
    assert records[0]["error_code"] == 400
    assert records[0]["description"] == "Bad Request: chat not found"


def test_successful_send_writes_no_failure_record(monkeypatch, tmp_path) -> None:
    _reset_fake_client(monkeypatch)
    failures_file = tmp_path / "telegram_bridge_failures.jsonl"
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(store, ws_manager, failures_file=failures_file)
        await service.post_operator_queue_card(
            session_id="sess-1",
            reason="⚡️ Запросил оператора",
            last_message="хочу к менеджеру",
            client_label="Иван",
        )

    anyio.run(run)

    assert not failures_file.exists()


def test_failed_client_lead_card_send_writes_a_failure_record_with_session_id(monkeypatch, tmp_path) -> None:
    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["sendMessage"] = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: chat not found",
    }
    failures_file = tmp_path / "telegram_bridge_failures.jsonl"
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        service = _service(
            store, ws_manager, clients_topic_id="42", failures_file=failures_file
        )
        await service.post_client_lead_card("карточка лида", session_id="sess-2")

    anyio.run(run)

    from app.utils.jsonl import read_jsonl

    records = read_jsonl(failures_file)
    assert len(records) == 1
    assert records[0]["kind"] == "client_lead_card"
    assert records[0]["session_id"] == "sess-2"


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
    которые никто не забрал). Заголовок (2026-08-29): читаемый номер + эмодзи-тип + оператор
    — сознательно БЕЗ имени клиента (PII видно даже в превью уведомлений на заблокированном
    телефоне оператора; само имя никуда не девается — оно всё так же в лиде/архиве/сессии)."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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
    topic_name = create_calls[0]["json"]["name"]
    assert re.match(r"^#\d{9} 🟢 · masha$", topic_name), topic_name
    assert "Мария Петровна" not in topic_name

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert any("хочу оставить телефон" in call["json"]["text"] for call in send_calls)
    assert any("Взято в работу" in call["json"]["text"] for call in send_calls)


def test_claim_transitions_session_to_human_active(monkeypatch) -> None:
    """Живой баг (нагрузочный тест виджета через реальный сервер, 2026-08-25): клейм менял
    только telegram_claimed_by/telegram_topic_id, но не статус сессии — она навсегда
    оставалась в WAITING_OPERATOR, и каждое следующее сообщение клиента получало
    "администратор подключается" вместо реального разговора, даже когда оператор уже
    реально взял диалог в работу через кнопку в Telegram."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> SessionStatus:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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
        updated = await store.get(session.session_id)
        return updated.status

    status = anyio.run(run)
    assert status == SessionStatus.HUMAN_ACTIVE


def test_claim_tracks_operator_claimed_analytics_event(monkeypatch) -> None:
    """Аналитика "по манагерам" (в разработке, 2026-08-27) без этого события не имеет
    источника данных — session.telegram_claimed_by живёт только в памяти и стирается
    вместе с TTL-эвикцией сессии."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics = FakeAnalyticsService()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager, analytics_service=analytics)

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

    claimed_events = [e for e in analytics.events if e["event_type"] == "operator_claimed"]
    assert len(claimed_events) == 1
    assert claimed_events[0]["session_id"] == session_id
    assert claimed_events[0]["company_id"] == "rosh_demo"
    assert claimed_events[0]["metadata"]["claimed_by"] == "masha"


def test_claim_without_analytics_service_does_not_crash(monkeypatch) -> None:
    """analytics_service опционален (None в проде, если не передали) — клейм не должен
    падать без него, старое поведение остаётся рабочим."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> SessionStatus:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager)  # analytics_service не передан — None по умолчанию

        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )
        updated = await store.get(session.session_id)
        return updated.status

    status = anyio.run(run)
    assert status == SessionStatus.HUMAN_ACTIVE


def test_close_tracks_operator_closed_event_with_claimed_by(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics = FakeAnalyticsService()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42, claimed_by="masha")
        await store.set_status(session.session_id, SessionStatus.HUMAN_ACTIVE)
        service = _service(store, ws_manager, analytics_service=analytics)

        await service._close_session_from_topic(session.session_id, 42)
        return session.session_id

    session_id = anyio.run(run)

    closed_events = [e for e in analytics.events if e["event_type"] == "operator_closed"]
    assert len(closed_events) == 1
    assert closed_events[0]["session_id"] == session_id
    assert closed_events[0]["metadata"]["claimed_by"] == "masha"


def test_close_skips_operator_closed_event_when_never_claimed(monkeypatch) -> None:
    """Диалог мог закрыться, не будучи ни разу взятым в работу (например, клиент сам ушёл) —
    не должно быть operator_closed без соответствующего operator_claimed, иначе аналитика
    "по манагерам" считает несуществующего оператора."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics = FakeAnalyticsService()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager, analytics_service=analytics)
        await service._close_session_from_topic(session.session_id, 42)

    anyio.run(run)

    assert analytics.events == []


def test_close_evicted_operator_session_records_operator_closed_and_closes_topic(monkeypatch) -> None:
    """Живой баг (код-ревью, 2026-08-27; докручено 2026-08-29): оператор взял диалог и забыл
    про него — сессия дошла до TTL-эвикции (session_store.evict_stale) без явного /done.
    Раньше фиксировали только operator_closed в аналитику — сама тема в Telegram оставалась
    висеть открытой навсегда, никто не уведомлялся. Сессия намеренно удалена из store ДО
    вызова — ровно как в реальной эвикции (evict_stale удаляет раньше, чем этот метод
    вызывается) — иначе тест не поймал бы баг, при котором close_topic сам перечитывает
    сессию из store и тихо ничего не закрывает."""

    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["closeForumTopic"] = {"ok": True, "result": True}
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics = FakeAnalyticsService()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42, claimed_by="masha")
        await store.set_status(session.session_id, SessionStatus.HUMAN_ACTIVE)
        service = _service(store, ws_manager, analytics_service=analytics)
        evicted_session = await store.get(session.session_id)
        store._sessions.pop(session.session_id, None)  # симулируем реальную эвикцию
        await service.close_evicted_operator_session(evicted_session)
        return session.session_id

    session_id = anyio.run(run)

    closed_events = [e for e in analytics.events if e["event_type"] == "operator_closed"]
    assert len(closed_events) == 1
    assert closed_events[0]["session_id"] == session_id
    assert closed_events[0]["metadata"]["claimed_by"] == "masha"

    send_calls = [c for c in FakeAsyncClient.calls if c["method"] == "sendMessage"]
    assert len(send_calls) == 1
    assert "неактивности" in send_calls[0]["json"]["text"]
    assert send_calls[0]["json"]["message_thread_id"] == 42

    close_calls = [c for c in FakeAsyncClient.calls if c["method"] == "closeForumTopic"]
    assert len(close_calls) == 1
    assert close_calls[0]["json"]["message_thread_id"] == 42


def test_close_evicted_operator_session_skips_unclaimed_or_non_operator_session(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics = FakeAnalyticsService()

    async def run() -> None:
        service = _service(store, ws_manager, analytics_service=analytics)

        never_claimed = await store.get_or_create(None, "rosh_demo")
        await store.set_status(never_claimed.session_id, SessionStatus.WAITING_OPERATOR)
        await service.close_evicted_operator_session(await store.get(never_claimed.session_id))

        ai_active = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(ai_active.session_id, topic_id=7, claimed_by="masha")
        await service.close_evicted_operator_session(await store.get(ai_active.session_id))

    anyio.run(run)

    assert analytics.events == []
    assert FakeAsyncClient.calls == []


def test_claim_does_not_reopen_already_closed_session(monkeypatch) -> None:
    """Клик по устаревшей кнопке "Взять в работу" на уже закрытом диалоге не должен
    воскрешать сессию в HUMAN_ACTIVE — симметрично защите в routes/operator.py:take_session."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> SessionStatus:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.CLOSED)
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
        updated = await store.get(session.session_id)
        return updated.status

    status = anyio.run(run)
    assert status == SessionStatus.CLOSED


def test_claim_on_ai_active_session_is_rejected_without_hijacking(monkeypatch) -> None:
    """Клиент уже вернулся к боту (см. operator_wait_timeout_offer) до того, как оператор
    успел нажать "Взять в работу" на устаревшей карточке — клик не должен молча выдёргивать
    живой AI-диалог обратно в HUMAN_ACTIVE."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> tuple[SessionStatus, str | None]:
        session = await store.get_or_create(None, "rosh_demo")
        assert session.status == SessionStatus.AI_ACTIVE
        service = _service(store, ws_manager)

        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )
        updated = await store.get(session.session_id)
        return updated.status, updated.telegram_claimed_by

    status, claimed_by = anyio.run(run)
    assert status == SessionStatus.AI_ACTIVE
    assert claimed_by is None
    answer_calls = [call for call in FakeAsyncClient.calls if call["method"] == "answerCallbackQuery"]
    assert len(answer_calls) == 1
    assert "вернулся к боту" in answer_calls[0]["json"]["text"]


def test_claim_attaches_close_button_and_pins_it(monkeypatch) -> None:
    """UX-удобство (запрос оператора, 2026-08-24): набирать /done руками неудобно, особенно
    с телефона. "Взято в работу" теперь несёт inline-кнопку "Завершить диалог", закреплённую
    в теме — доступна без прокрутки даже в длинной переписке."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        FakeAsyncClient.responses["sendMessage"] = {"ok": True, "result": {"message_id": 777}}
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

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    claimed_call = next(call for call in send_calls if "Взято в работу" in call["json"]["text"])
    keyboard = claimed_call["json"]["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["text"] == "✅ Завершить диалог"
    assert keyboard[0][0]["callback_data"] == f"close:{session_id}"

    pin_calls = [call for call in FakeAsyncClient.calls if call["method"] == "pinChatMessage"]
    assert len(pin_calls) == 1
    assert pin_calls[0]["json"]["message_id"] == 777


def test_close_button_callback_closes_session_same_as_slash_command(monkeypatch) -> None:
    """Кнопка "Завершить диалог" должна переиспользовать тот же _close_session_from_topic,
    что и /done — идентичное поведение, просто другой способ его вызвать."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)
        await service._handle_callback_query(
            {"id": "cb2", "data": f"close:{session.session_id}", "from": {"username": "masha"}}
        )
        return session.session_id

    session_id = anyio.run(run)

    assert ws_manager.disconnect_operator_calls == [{"session_id": session_id, "close_session": True}]

    close_calls = [call for call in FakeAsyncClient.calls if call["method"] == "closeForumTopic"]
    assert len(close_calls) == 1
    assert close_calls[0]["json"]["message_thread_id"] == 42

    answer_calls = [call for call in FakeAsyncClient.calls if call["method"] == "answerCallbackQuery"]
    assert any(call["json"]["callback_query_id"] == "cb2" for call in answer_calls)


def test_claim_topic_name_is_the_same_shape_with_or_without_known_contact(monkeypatch) -> None:
    """2026-08-29: заголовок темы больше не зависит от того, известно ли имя/телефон
    клиента — раньше был отдельный фолбэк "Сессия <id>", теперь формат один и тот же всегда
    (номер+эмодзи+оператор), потому что персональные данные в заголовке не показываем вообще."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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
    assert re.match(r"^#\d{9} 🟢 · masha$", create_calls[0]["json"]["name"])


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
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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


def test_format_transcript_keeps_all_messages_when_under_budget() -> None:
    store = SessionStore()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.append_message(session.session_id, MessageRole.USER, "хочу оставить телефон")
        await store.append_message(session.session_id, MessageRole.ASSISTANT, "Хорошо, оставьте контакт")
        session = await store.get(session.session_id)
        return telegram_bridge_module._format_transcript(session)

    transcript = anyio.run(run)

    lines = transcript.splitlines()
    assert len(lines) == 2
    assert "хочу оставить телефон" in lines[0]
    assert "Хорошо, оставьте контакт" in lines[1]


def test_format_transcript_drops_oldest_messages_over_char_budget() -> None:
    """Живой баг (ручное тестирование, 2026-08-26): жёсткий лимит в 15 СООБЩЕНИЙ в длинном
    чате с болтливой пляской вокруг оператора съедал весь бюджет на неё, вытесняя исходные
    содержательные вопросы клиента в начале диалога. Бюджет по символам вместо количества —
    новые (самые релевантные для оператора) сообщения должны остаться, старые уйти."""

    store = SessionStore()
    budget = telegram_bridge_module._TELEGRAM_TRANSCRIPT_CHAR_BUDGET

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        # каждое сообщение длиннее бюджета/10 — гарантированно не влезут все разом
        for index in range(40):
            await store.append_message(
                session.session_id, MessageRole.USER, f"сообщение номер {index} " + "x" * 100
            )
        session = await store.get(session.session_id)
        return telegram_bridge_module._format_transcript(session)

    transcript = anyio.run(run)

    assert len(transcript) <= budget
    assert "сообщение номер 39" in transcript  # самое новое — сохранилось
    assert "сообщение номер 0 " not in transcript  # самое старое — вытеснено


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


def test_notify_client_left_posts_warning_and_closes_topic(monkeypatch) -> None:
    """Клиент сам сбросил диалог кнопкой в виджете (см. /api/chat/session/{id}/cancel) —
    оператор должен увидеть это в теме, а не продолжать печатать в пустоту."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_telegram_bridge(session.session_id, topic_id=42)
        service = _service(store, ws_manager)
        await service.notify_client_left(session.session_id)
        return session.session_id

    anyio.run(run)

    send_calls = [call for call in FakeAsyncClient.calls if call["method"] == "sendMessage"]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["message_thread_id"] == 42
    assert "покинул чат" in send_calls[0]["json"]["text"]

    close_calls = [call for call in FakeAsyncClient.calls if call["method"] == "closeForumTopic"]
    assert len(close_calls) == 1
    assert close_calls[0]["json"]["message_thread_id"] == 42


def test_notify_client_left_is_noop_without_topic(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run() -> None:
        session = await store.get_or_create(None, "rosh_demo")
        service = _service(store, ws_manager)
        await service.notify_client_left(session.session_id)

    anyio.run(run)

    assert FakeAsyncClient.calls == []


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
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
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


def test_claim_topic_emoji_reflects_last_intent(monkeypatch) -> None:
    """2026-08-29: эмодзи в заголовке темы — по session.last_intent (то же поле, что уже
    используется в "Разбивка по темам" в аналитике), не по имени/тексту заново."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()

    async def run(last_intent: str) -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        await store.update_context(session.session_id, last_intent=last_intent)
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
        create_calls = [c for c in FakeAsyncClient.calls if c["method"] == "createForumTopic"]
        return create_calls[-1]["json"]["name"]

    complaint_name = anyio.run(run, "complaint")
    assert "🔴" in complaint_name

    FakeAsyncClient.calls = []
    crisis_name = anyio.run(run, "self_harm_crisis")
    assert "🆘" in crisis_name

    FakeAsyncClient.calls = []
    regulated_name = anyio.run(run, "regulated_advice")
    assert "🟡" in regulated_name

    FakeAsyncClient.calls = []
    plain_name = anyio.run(run, "operator_requested")
    assert "🟢" in plain_name


def test_claim_topic_index_increments_from_todays_claims_in_analytics_file(tmp_path, monkeypatch) -> None:
    """2026-08-29: номер темы за сегодня считается из уже накопленных operator_claimed за
    сегодня в analytics.jsonl (не отдельный счётчик в памяти — тот пропал бы при рестарте и
    задваивал бы номера)."""

    from datetime import datetime, timezone

    from app.analytics import AnalyticsService
    from app.utils.jsonl import append_jsonl

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws_manager = FakeWsManager()
    analytics_file = tmp_path / "analytics.jsonl"
    today = datetime.now(timezone.utc)
    # 2 уже "случившихся" сегодня клейма — новый должен получить номер 3.
    for _ in range(2):
        append_jsonl(
            analytics_file,
            {
                "timestamp": today.isoformat(),
                "company_id": "rosh_demo",
                "session_id": "some-other-session",
                "event_type": "operator_claimed",
                "message": "",
                "metadata": {},
            },
        )
    analytics_service = AnalyticsService(analytics_file=analytics_file, leads_file=tmp_path / "leads.jsonl")

    async def run() -> str:
        session = await store.get_or_create(None, "rosh_demo")
        await store.set_status(session.session_id, SessionStatus.WAITING_OPERATOR)
        FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}
        service = _service(store, ws_manager, analytics_service=analytics_service)
        await service._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session.session_id}",
                "from": {"username": "masha"},
                "message": {"message_id": 100},
            }
        )

    anyio.run(run)

    create_calls = [c for c in FakeAsyncClient.calls if c["method"] == "createForumTopic"]
    topic_name = create_calls[0]["json"]["name"]
    assert re.match(r"^#\d{6}003 🟢 · masha$", topic_name), topic_name
