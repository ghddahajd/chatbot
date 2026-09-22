"""группа операторов Telegram на клиента (шаг 4 дорожной карты, 2026-09-23).

Главное, что проверяется: карточки клиента Б уходят только в группу Б; сообщение оператора из
группы Б не попадает посетителю клиента А, даже если номер темы совпал; без карты всё как раньше;
плавный переход — темы, начатые до карты, продолжают работать."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from app.models import SessionStatus
from app.sessions import SessionStore
from app.telegram_bridge import TelegramBridgeService
from app.telegram_routing import INVALID, LEGACY, MAP, TelegramRouting, TelegramTarget

from .test_telegram_bridge import FakeAsyncClient, FakeWsManager, _reset_fake_client

LEGACY_GROUP = "-100111"
GROUP_A = "-100111"  # переход, шаг 2: РОШ в карте с ТОЙ ЖЕ группой, что была общей
GROUP_B = "-100222"
CLIENT_MAP = json.dumps(
    {
        "rosh_demo": {"group": GROUP_A, "topic": "5"},
        "clinic_b": {"group": GROUP_B, "topic": "9"},
    }
)


# ---------------------------------------------------------------- правила маршрутизации


def test_empty_map_is_legacy_everyone_goes_to_the_common_group() -> None:
    routing = TelegramRouting(legacy_group_id=LEGACY_GROUP, legacy_clients_topic_id="5")

    assert routing.mode == LEGACY
    assert routing.target_for("rosh_demo") == TelegramTarget(LEGACY_GROUP, "5")
    assert routing.target_for("anything") == TelegramTarget(LEGACY_GROUP, "5")
    assert routing.groups() == {LEGACY_GROUP: ["*"]}


def test_map_routes_each_client_and_nobody_else() -> None:
    routing = TelegramRouting(legacy_group_id=LEGACY_GROUP, client_groups_raw=CLIENT_MAP)

    assert routing.mode == MAP
    assert routing.target_for("rosh_demo") == TelegramTarget(GROUP_A, "5")
    assert routing.target_for("clinic_b") == TelegramTarget(GROUP_B, "9")
    assert routing.target_for("clinic_c") is None  # не из карты — никуда, не в общую группу
    assert routing.target_for(None) is None
    assert routing.groups() == {GROUP_A: ["rosh_demo"], GROUP_B: ["clinic_b"]}
    assert routing.is_known_group(GROUP_B) and routing.is_known_group(LEGACY_GROUP)
    assert not routing.is_known_group("-100999") and not routing.is_known_group(None)


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ('{"rosh_demo": {"group": "-100111"', "не JSON"),
        ("[]", "непустой объект"),
        ("{}", "непустой объект"),
        ('{"rosh_demo": "-100111"}', "объект с полем group"),
        ('{"rosh_demo": {"topic": "5"}}', "нет числового group"),
        ('{"rosh_demo": {"group": "abc"}}', "нет числового group"),
        ('{"rosh_demo": {"group": "-100111", "topic": "пять"}}', "topic должен быть числом"),
        ('{"../evil": {"group": "-100111"}}', "странный id клиента"),
    ],
)
def test_broken_map_sends_nothing_new_and_says_why(raw: str, needle: str) -> None:
    routing = TelegramRouting(legacy_group_id=LEGACY_GROUP, client_groups_raw=raw)

    assert routing.mode == INVALID
    assert needle in routing.error
    assert routing.target_for("rosh_demo") is None  # не «откатываемся на общую группу» — там чужие
    assert routing.is_known_group(LEGACY_GROUP)  # но старые темы в общей группе продолжают работать


# ---------------------------------------------------------------- бридж с картой групп


def _service(store: SessionStore, ws: FakeWsManager | None = None, *, raw: str = CLIENT_MAP, failures_file: Path | None = None, analytics: Any = None) -> TelegramBridgeService:
    return TelegramBridgeService(
        bot_token="token",
        group_chat_id=LEGACY_GROUP,
        session_store=store,
        ws_manager=ws or FakeWsManager(),
        clients_topic_id="5",
        failures_file=failures_file,
        analytics_service=analytics,
        routing=TelegramRouting(legacy_group_id=LEGACY_GROUP, legacy_clients_topic_id="5", client_groups_raw=raw),
    )


def _calls(method: str) -> list[dict[str, Any]]:
    return [call["json"] for call in FakeAsyncClient.calls if call["method"] == method]


def test_queue_cards_go_to_each_clients_own_group(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store)

    async def run() -> None:
        a = await store.get_or_create("sess-a", "rosh_demo")
        b = await store.get_or_create("sess-b", "clinic_b")
        await service.post_operator_queue_card(session_id=a.session_id, reason="⚡️", last_message="привет", client_label="А")
        await service.post_operator_queue_card(session_id=b.session_id, reason="⚡️", last_message="привет", client_label="Б")

    anyio.run(run)

    assert [call["chat_id"] for call in _calls("sendMessage")] == [GROUP_A, GROUP_B]


def test_client_without_group_gets_nothing_and_it_is_logged(monkeypatch, tmp_path: Path) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    failures = tmp_path / "tg_failures.jsonl"
    service = _service(store, failures_file=failures)

    async def run() -> None:
        c = await store.get_or_create("sess-c", "clinic_c")
        await service.post_operator_queue_card(session_id=c.session_id, reason="⚡️", last_message="x", client_label="В")
        await service.post_client_lead_card("карточка", session_id=c.session_id)

    anyio.run(run)

    assert _calls("sendMessage") == []  # ни в чью группу
    records = [json.loads(line) for line in failures.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == ["operator_queue_card", "client_lead_card"]
    assert "clinic_c" in records[0]["description"]


def test_lead_card_goes_to_the_clients_own_leads_topic(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store)

    async def run() -> None:
        b = await store.get_or_create("sess-b", "clinic_b")
        await service.post_client_lead_card("карточка лида", session_id=b.session_id)

    anyio.run(run)

    call = _calls("sendMessage")[0]
    assert call["chat_id"] == GROUP_B and call["message_thread_id"] == 9


def _claim(session_id: str, chat_id: str) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb1",
            "data": f"claim:{session_id}",
            "from": {"first_name": "Оля"},
            "message": {"message_id": 77, "chat": {"id": int(chat_id)}},
        }
    }


def test_claim_creates_topic_in_the_clients_group_and_remembers_it(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 7}}
    store = SessionStore()
    service = _service(store)

    async def run():
        b = await store.get_or_create("sess-b", "clinic_b")
        await store.set_status(b.session_id, SessionStatus.WAITING_OPERATOR)
        await service._process_update(_claim(b.session_id, GROUP_B))
        return await store.get(b.session_id)

    session = anyio.run(run)

    assert _calls("createForumTopic")[0]["chat_id"] == GROUP_B
    assert session.telegram_topic_id == 7 and session.telegram_group_id == GROUP_B
    assert session.status == SessionStatus.HUMAN_ACTIVE


def test_claim_button_pressed_in_another_clients_group_is_refused(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store)

    async def run():
        b = await store.get_or_create("sess-b", "clinic_b")
        await store.set_status(b.session_id, SessionStatus.WAITING_OPERATOR)
        await service._process_update(_claim(b.session_id, GROUP_A))  # оператор РОШ жмёт кнопку чужого клиента
        return await store.get(b.session_id)

    session = anyio.run(run)

    assert _calls("createForumTopic") == []
    assert session.telegram_claimed_by is None and session.status == SessionStatus.WAITING_OPERATOR
    assert _calls("answerCallbackQuery")[0]["text"] == "Сессия не найдена"


def _operator_message(chat_id: str, thread_id: int, text: str) -> dict[str, Any]:
    return {"message": {"message_thread_id": thread_id, "text": text, "chat": {"id": int(chat_id)}}}


def test_same_topic_number_in_two_groups_reaches_only_the_right_visitor(monkeypatch) -> None:
    """главный риск шага: номер темы уникален только внутри группы."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws = FakeWsManager()
    service = _service(store, ws)

    async def run() -> None:
        a = await store.get_or_create("sess-a", "rosh_demo")
        b = await store.get_or_create("sess-b", "clinic_b")
        await store.set_telegram_bridge(a.session_id, topic_id=7, group_id=GROUP_A)
        await store.set_telegram_bridge(b.session_id, topic_id=7, group_id=GROUP_B)
        await service._process_update(_operator_message(GROUP_B, 7, "здравствуйте, это клиника Б"))

    anyio.run(run)

    assert [session_id for session_id, _payload in ws.sent] == ["sess-b"]


def test_messages_from_unknown_groups_are_ignored(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws = FakeWsManager()
    service = _service(store, ws)

    async def run() -> None:
        a = await store.get_or_create("sess-a", "rosh_demo")
        await store.set_telegram_bridge(a.session_id, topic_id=7, group_id=GROUP_A)
        await service._process_update(_operator_message("-100999", 7, "чужая группа"))

    anyio.run(run)

    assert ws.sent == []


def test_topic_started_before_the_map_keeps_working(monkeypatch) -> None:
    """плавный переход: у сессии, взятой в работу до включения карты, группа не записана —
    её тема живёт в старой общей группе, и переписка там продолжается."""

    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws = FakeWsManager()
    service = _service(store, ws, raw=json.dumps({"clinic_b": {"group": GROUP_B}}))  # РОШ из карты даже убран

    async def run() -> None:
        old = await store.get_or_create("sess-old", "rosh_demo")
        await store.set_telegram_bridge(old.session_id, topic_id=5)  # без group_id — как до деплоя
        await service._process_update(_operator_message(LEGACY_GROUP, 5, "продолжаем"))
        await service.forward_client_message(old.session_id, "ответ клиента")

    anyio.run(run)

    assert [session_id for session_id, _payload in ws.sent] == ["sess-old"]
    assert _calls("sendMessage")[0]["chat_id"] == LEGACY_GROUP


def test_forward_and_close_use_the_topics_own_group(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    ws = FakeWsManager()
    service = _service(store, ws)

    async def run() -> None:
        b = await store.get_or_create("sess-b", "clinic_b")
        await store.set_telegram_bridge(b.session_id, topic_id=7, group_id=GROUP_B, claimed_by="Оля")
        await service.forward_client_message(b.session_id, "вопрос")
        await service._process_update(_operator_message(GROUP_B, 7, "/done"))

    anyio.run(run)

    assert all(call["chat_id"] == GROUP_B for call in _calls("sendMessage"))
    assert _calls("closeForumTopic")[0]["chat_id"] == GROUP_B


def test_close_button_from_another_group_is_refused(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    store = SessionStore()
    service = _service(store)

    async def run():
        b = await store.get_or_create("sess-b", "clinic_b")
        await store.set_telegram_bridge(b.session_id, topic_id=7, group_id=GROUP_B)
        callback = {"callback_query": {"id": "cb2", "data": f"close:{b.session_id}", "message": {"chat": {"id": int(GROUP_A)}}}}
        await service._process_update(callback)

    anyio.run(run)

    assert _calls("closeForumTopic") == []


class _Analytics:
    def __init__(self, path: Path) -> None:
        self.analytics_file = path


def test_topic_numbers_are_counted_per_client_in_map_mode(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    path = tmp_path / "analytics.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"event_type": "operator_claimed", "company_id": "rosh_demo", "timestamp": now}] * 3
    rows += [{"event_type": "operator_claimed", "company_id": "clinic_b", "timestamp": now}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    # «сегодня» — по Москве, как в _topic_display_index (иначе ночью по МСК это ещё «вчера» по UTC)
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Moscow"))

    mapped = _service(SessionStore(), analytics=_Analytics(path))
    legacy = _service(SessionStore(), raw="", analytics=_Analytics(path))

    assert mapped._next_daily_topic_index(today, "clinic_b") == 1
    assert mapped._next_daily_topic_index(today, "rosh_demo") == 3
    assert legacy._next_daily_topic_index(today) == 4  # без карты — общая нумерация, как раньше


def test_health_check_reports_every_group(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["getMe"] = {"ok": True, "result": {"id": 1, "username": "rosh_bot"}}
    FakeAsyncClient.responses["getChatMember"] = [
        {"ok": True, "result": {"status": "administrator"}},
        {"ok": True, "result": {"status": "kicked"}},
    ]
    service = _service(SessionStore())

    result = anyio.run(service.health_check)

    assert result["mode"] == MAP
    by_group = {group["group"]: group for group in result["groups"]}
    assert set(by_group) == {GROUP_A, GROUP_B}
    assert sorted(group["status"] for group in result["groups"]) == ["error", "ok"]
    assert result["operators_group"]["status"] == "error"
    broken = next(group for group in result["groups"] if group["status"] == "error")
    assert broken["companies"][0] in result["operators_group"]["detail"]


def test_health_check_with_a_broken_map_says_why(monkeypatch) -> None:
    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["getMe"] = {"ok": True, "result": {"id": 1, "username": "rosh_bot"}}
    service = _service(SessionStore(), raw="{oops")

    result = anyio.run(service.health_check)

    assert result["mode"] == INVALID
    assert result["operators_group"]["status"] == "error"
    assert "не JSON" in result["operators_group"]["detail"]


def test_session_snapshot_without_group_field_still_loads(tmp_path: Path) -> None:
    """снапшот сессий, записанный до этой версии, не содержит telegram_group_id."""

    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps([{"session_id": "s1", "company_id": "rosh_demo", "status": "HUMAN_ACTIVE", "telegram_topic_id": 5}]),
        encoding="utf-8",
    )
    store = SessionStore()

    async def run():
        await store.restore_from(path)
        return await store.get("s1")

    session = anyio.run(run)

    assert session is not None and session.telegram_topic_id == 5 and session.telegram_group_id is None


# ---------------------------------------------------------------- preflight


def test_preflight_lists_clients_without_group(test_client, monkeypatch) -> None:
    """в фикстуре три клиента (rosh_demo, dup_one, dup_two); в карте только rosh_demo."""

    raw = json.dumps({"rosh_demo": {"group": GROUP_A, "topic": "5"}})
    service = TelegramBridgeService(
        bot_token="token", group_chat_id=LEGACY_GROUP, session_store=SessionStore(), ws_manager=FakeWsManager(),
        routing=TelegramRouting(legacy_group_id=LEGACY_GROUP, client_groups_raw=raw),
    )
    monkeypatch.setattr(test_client.app.state, "telegram_bridge_service", service)

    telegram = test_client.get(
        "/api/debug/preflight?network=0", headers={"X-Operator-Token": "demo-operator-token"}
    ).json()["checks"]["telegram"]

    assert telegram["status"] == "degraded"
    assert telegram["routing"]["mode"] == MAP
    assert telegram["routing"]["clients_without_group"] == ["dup_one", "dup_two"]
    assert "dup_one" in telegram["detail"]


def test_preflight_broken_map_is_an_error(test_client, monkeypatch) -> None:
    service = TelegramBridgeService(
        bot_token="token", group_chat_id=LEGACY_GROUP, session_store=SessionStore(), ws_manager=FakeWsManager(),
        routing=TelegramRouting(legacy_group_id=LEGACY_GROUP, client_groups_raw="{oops"),
    )
    monkeypatch.setattr(test_client.app.state, "telegram_bridge_service", service)

    telegram = test_client.get(
        "/api/debug/preflight?network=0", headers={"X-Operator-Token": "demo-operator-token"}
    ).json()["checks"]["telegram"]

    assert telegram["status"] == "error"
    assert "карта групп с ошибкой" in telegram["detail"]
