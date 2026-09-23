"""сбой связи с Telegram (случай 2026-09-15: сервер жив, чат отвечает, а до оператора не
достучаться): бот честно просит телефон вместо «передаю менеджеру», неушедшие карточки ждут
в очереди на диске и досылаются, когда связь вернулась."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.telegram_bridge import TelegramBridgeService

DOWN = {"ok": False, "description": "network_error:ConnectError"}


class _Telegram:
    """подменный Telegram: up=False — всё падает, как 15.09."""

    def __init__(self) -> None:
        self.up = False
        self.calls: list[dict] = []

    async def __call__(self, method, **params):
        self.calls.append({"method": method, **params})
        return {"ok": True, "result": {"message_id": len(self.calls)}} if self.up else DOWN


@pytest.fixture()
def outage(test_client, managed_env):
    telegram = _Telegram()
    bridge = TelegramBridgeService(
        bot_token="123:test",
        group_chat_id="-1001",
        session_store=test_client.app.state.session_store,
        ws_manager=test_client.app.state.ws_manager,
        clients_topic_id="7",
        failures_file=managed_env["temp_dir"] / "tg_failures.jsonl",
        pending_file=managed_env["temp_dir"] / "tg_pending.jsonl",
    )
    bridge._call = telegram
    test_client.app.state.telegram_bridge_service = bridge
    company = test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company
    company.working_hours_schedule = {}  # всегда открыто — ночью оператора и так не зовём
    return bridge, telegram


def _chat(test_client, message: str, session_id: str | None = None) -> dict:
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message})
    assert response.status_code == 200
    return response.json()


def _ask_operator(test_client, phrase: str = "позовите оператора") -> dict:
    """как в жизни: бот сначала предлагает помочь сам, «да» — соединяет."""

    offer = _chat(test_client, phrase)
    return _chat(test_client, "да", offer["session_id"])


def _pending(bridge) -> list[dict]:
    path = bridge.pending_file
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_operator_request_during_outage_is_honest_and_asks_for_phone(test_client, outage) -> None:
    bridge, telegram = outage

    payload = _ask_operator(test_client)

    assert payload["answer"].startswith("Не получилось сразу связаться")
    assert "Передаю" not in payload["answer"]
    assert payload["status"] == "AI_ACTIVE"
    assert payload["action"] == "ask_contact"
    assert [entry["kind"] for entry in _pending(bridge)] == ["operator_request"]
    # клиент не ждёт минутами: короткий бюджет и одна повторная попытка
    first = next(call for call in telegram.calls if call["method"] == "sendMessage")
    assert first["_http_timeout"] == 5.0 and first["_max_retries"] == 1


def test_clinic_phone_is_offered_when_known(test_client, outage) -> None:
    phone = test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company.phone

    payload = _ask_operator(test_client)

    assert f"Или позвоните нам: {phone}." in payload["answer"]


def test_phone_after_outage_becomes_urgent_lead_and_is_resent_later(test_client, managed_env, outage) -> None:
    bridge, telegram = outage
    first = _ask_operator(test_client)

    lead_payload = _chat(test_client, "+7 900 000-00-05", first["session_id"])

    assert lead_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["needs_operator"] is True
    assert sorted(entry["kind"] for entry in _pending(bridge)) == ["lead", "operator_request"]

    telegram.up = True
    telegram.calls.clear()
    sent = test_client.portal.call(bridge.resend_pending_cards)

    assert sent == 1  # заметка о пропущенном запросе не нужна — телефон оставлен, досылаем заявку
    text = telegram.calls[-1]["text"]
    assert text.startswith("⏳ Доставлено с опозданием")
    assert "+79000000005" in text or "900" in text
    assert "reply_markup" not in telegram.calls[-1]  # без «Взять в работу»: дело — перезвонить
    assert _pending(bridge) == []


def test_missed_operator_request_without_phone_becomes_a_note(test_client, outage) -> None:
    bridge, telegram = outage
    _ask_operator(test_client)
    telegram.up = True
    telegram.calls.clear()

    assert test_client.portal.call(bridge.resend_pending_cards) == 1
    call = telegram.calls[-1]
    assert "Пропущенный запрос оператора" in call["text"]
    assert "Контакт не оставил" in call["text"]
    assert call["message_thread_id"] == 7
    assert "reply_markup" not in call


def test_still_down_keeps_the_queue_and_does_not_hammer(test_client, outage) -> None:
    bridge, telegram = outage
    _ask_operator(test_client)
    _ask_operator(test_client, "позовите менеджера")  # вторая сессия — вторая карточка в очереди
    telegram.calls.clear()

    assert test_client.portal.call(bridge.resend_pending_cards) == 0
    assert len(telegram.calls) == 1  # первая неудача — ждём следующего круга
    assert len(_pending(bridge)) == 2


def test_cards_older_than_a_day_are_dropped_to_the_failures_log(test_client, managed_env, outage) -> None:
    bridge, telegram = outage
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).replace(tzinfo=None).isoformat()
    bridge.pending_file.write_text(json.dumps({"id": "x", "kind": "lead", "session_id": "s-old", "created_at": old, "text": "t"}) + "\n", encoding="utf-8")
    telegram.up = True

    assert test_client.portal.call(bridge.resend_pending_cards) == 0
    assert telegram.calls == []
    assert _pending(bridge) == []
    assert "pending_expired:lead" in (managed_env["temp_dir"] / "tg_failures.jsonl").read_text(encoding="utf-8")


def test_queue_survives_restart(test_client, managed_env, outage) -> None:
    bridge, telegram = outage
    _ask_operator(test_client)
    restarted = TelegramBridgeService(
        bot_token="123:test",
        group_chat_id="-1001",
        session_store=test_client.app.state.session_store,
        ws_manager=test_client.app.state.ws_manager,
        clients_topic_id="7",
        pending_file=bridge.pending_file,
    )
    telegram.up = True
    restarted._call = telegram

    assert test_client.portal.call(restarted.resend_pending_cards) == 1


def test_when_telegram_is_up_nothing_changes(test_client, outage) -> None:
    bridge, telegram = outage
    telegram.up = True

    payload = _ask_operator(test_client)

    assert payload["action"] == "transfer_operator"
    assert payload["status"] == "WAITING_OPERATOR"
    assert "Не получилось" not in payload["answer"]
    assert _pending(bridge) == []


def test_preflight_shows_the_pending_queue(test_client, outage) -> None:
    from app.preflight import check_telegram

    _ask_operator(test_client)

    item = test_client.portal.call(lambda: check_telegram(test_client.app, include_network=False))
    assert item["pending_cards"] == 1
    assert "в очереди на досылку: 1" in item["detail"]
