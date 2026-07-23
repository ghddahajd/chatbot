"""проверки generic delivery outbox."""

import asyncio
from pathlib import Path
from typing import Any

from app import delivery as delivery_module
from app.delivery import DeliveryService
from app.delivery import _telegram_text, _webhook_payload
from app.models import Lead
from app.utils.jsonl import read_jsonl


def _write_notifications_config(path: Path, chat_id: str, events: list[str]) -> None:
    path.write_text(
        "\n".join(
            [
                "notifications:",
                "  telegram:",
                "    enabled: true",
                f'    chat_id: "{chat_id}"',
                "    events:",
                *[f'      - "{event}"' for event in events],
                "  webhook:",
                "    enabled: false",
                '    url: ""',
                '    secret: ""',
                "    events:",
                '      - "lead_created"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_enqueue_event_creates_record_with_event_type(tmp_path: Path, resolver, monkeypatch) -> None:
    outbox_file = tmp_path / "delivery_outbox.jsonl"
    service = DeliveryService(
        outbox_file=outbox_file,
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )

    async def fake_send(record: dict[str, Any]) -> int:
        assert record["event_type"] == "operator_requested"
        return 200

    monkeypatch.setattr(service, "_send", fake_send)

    async def run_enqueue_event() -> list[dict[str, Any]]:
        return await service.enqueue_event(
            event_type="operator_requested",
            company_id="rosh_demo",
            session_id="session-1",
            payload={"last_message": "позовите оператора"},
        )

    import anyio

    records = anyio.run(run_enqueue_event)

    assert records[0]["status"] == "sent"
    outbox_records = read_jsonl(outbox_file)
    assert outbox_records[0]["event_type"] == "operator_requested"
    assert outbox_records[0]["delivery_id"] == outbox_records[1]["delivery_id"]
    assert outbox_records[1]["status"] == "sent"


def test_enqueue_lead_keeps_backward_compat_event_type(tmp_path: Path, resolver, monkeypatch) -> None:
    outbox_file = tmp_path / "delivery_outbox.jsonl"
    service = DeliveryService(
        outbox_file=outbox_file,
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )

    async def fake_send(record: dict[str, Any]) -> int:
        return 200

    monkeypatch.setattr(service, "_send", fake_send)

    lead = Lead(
        company_id="rosh_demo",
        session_id="session-1",
        name="Иван",
        phone="+7 999 123-45-67",
        summary="Хочу записаться",
    )

    async def run_enqueue_lead() -> list[dict[str, Any]]:
        return await service.enqueue_lead(lead)

    import anyio

    records = anyio.run(run_enqueue_lead)

    assert records[0]["event_type"] == "lead_created"
    assert read_jsonl(outbox_file)[0]["event_type"] == "lead_created"


def test_enqueue_event_uses_client_telegram_chat_id(
    tmp_path: Path,
    managed_env: dict[str, Path],
    resolver,
    monkeypatch,
) -> None:
    _write_notifications_config(
        managed_env["clients_dir"] / "rosh_demo" / "config.yaml",
        "client-chat",
        ["lead_created"],
    )
    outbox_file = tmp_path / "delivery_outbox.jsonl"
    service = DeliveryService(
        outbox_file=outbox_file,
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="global-chat",
    )

    async def fake_send(record: dict[str, Any]) -> int:
        assert record["target"] == "client-chat"
        return 200

    monkeypatch.setattr(service, "_send", fake_send)

    async def run_enqueue_event() -> list[dict[str, Any]]:
        return await service.enqueue_event(
            event_type="lead_created",
            company_id="rosh_demo",
            session_id="session-1",
            payload={"summary": "лид"},
        )

    import anyio

    records = anyio.run(run_enqueue_event)

    assert records[0]["target"] == "client-chat"


def test_enqueue_event_filters_client_events(
    tmp_path: Path,
    managed_env: dict[str, Path],
    resolver,
) -> None:
    _write_notifications_config(
        managed_env["clients_dir"] / "rosh_demo" / "config.yaml",
        "client-chat",
        ["lead_created"],
    )
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="global-chat",
    )

    async def run_enqueue_event() -> list[dict[str, Any]]:
        return await service.enqueue_event(
            event_type="operator_requested",
            company_id="rosh_demo",
            session_id="session-1",
            payload={"last_message": "оператор"},
        )

    import anyio

    assert anyio.run(run_enqueue_event) == []


def test_enqueue_event_falls_back_to_global_telegram_when_notifications_missing(
    tmp_path: Path,
    resolver,
    monkeypatch,
) -> None:
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="global-chat",
    )

    async def fake_send(record: dict[str, Any]) -> int:
        assert record["target"] == "global-chat"
        return 200

    monkeypatch.setattr(service, "_send", fake_send)

    async def run_enqueue_event() -> list[dict[str, Any]]:
        return await service.enqueue_event(
            event_type="lead_created",
            company_id="rosh_demo",
            session_id="session-1",
            payload={"summary": "лид"},
        )

    import anyio

    records = anyio.run(run_enqueue_event)

    assert records[0]["target"] == "global-chat"


def test_enqueue_event_supports_different_client_chat_ids(
    tmp_path: Path,
    managed_env: dict[str, Path],
    resolver,
    monkeypatch,
) -> None:
    _write_notifications_config(
        managed_env["clients_dir"] / "rosh_demo" / "config.yaml",
        "rosh-chat",
        ["lead_created"],
    )
    _write_notifications_config(
        managed_env["clients_dir"] / "dup_one" / "config.yaml",
        "dup-chat",
        ["lead_created"],
    )
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="global-chat",
    )

    async def fake_send(record: dict[str, Any]) -> int:
        return 200

    monkeypatch.setattr(service, "_send", fake_send)

    async def run_enqueue_events() -> list[list[dict[str, Any]]]:
        rosh_records = await service.enqueue_event(
            event_type="lead_created",
            company_id="rosh_demo",
            session_id="session-1",
            payload={"summary": "rosh"},
        )
        dup_records = await service.enqueue_event(
            event_type="lead_created",
            company_id="dup_one",
            session_id="session-2",
            payload={"summary": "dup"},
        )
        return [rosh_records, dup_records]

    import anyio

    rosh_records, dup_records = anyio.run(run_enqueue_events)

    assert rosh_records[0]["target"] == "rosh-chat"
    assert dup_records[0]["target"] == "dup-chat"


def test_telegram_formatter_uses_event_specific_text() -> None:
    lead_text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={"name": "Иван", "phone": "+7999", "summary": "Хочу консультацию"},
    )
    booking_text = _telegram_text(
        event_type="booking_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={"name": "Иван", "phone": "+7999", "summary": "Хочу записаться"},
        service_name="Чистка лица",
    )
    operator_text = _telegram_text(
        event_type="operator_requested",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={"last_message": "позовите оператора", "operator_url": "http://localhost:8000/operator"},
    )

    assert "Новая заявка" in lead_text
    assert "Запись на услугу" in booking_text
    assert "Чистка лица" in booking_text
    assert "Клиент просит оператора" in operator_text
    assert "http://localhost:8000/operator" in operator_text


def test_telegram_booking_text_includes_dialog_link_when_present() -> None:
    text = _telegram_text(
        event_type="booking_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={
            "name": "Иван",
            "phone": "+7999",
            "summary": "Хочу записаться",
            "operator_url": "http://operator.local/dialog",
        },
        service_name="Чистка лица",
    )

    assert "Открыть диалог: http://operator.local/dialog" in text


def test_telegram_booking_text_omits_dialog_link_without_operator_url() -> None:
    text = _telegram_text(
        event_type="booking_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={"name": "Иван", "phone": "+7999", "summary": "Хочу записаться"},
        service_name="Чистка лица",
    )

    assert "Открыть диалог" not in text


def test_telegram_lead_text_includes_reason_service_and_dialog_link() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={
            "name": "Иван",
            "phone": "+7999",
            "summary": "Хочу узнать про эпиляцию",
            "reason": "price_question",
            "recent_messages": [
                {"role": "user", "text": "сколько стоит эпиляция"},
                {"role": "assistant", "text": "от 3000 рублей"},
            ],
            "operator_url": "http://localhost:8000/operator?session_id=abc",
        },
        service_name="Лазерная эпиляция",
    )

    assert "Услуга: Лазерная эпиляция" in text
    assert "Тип запроса: Цена" in text
    assert "Последнее сообщение: сколько стоит эпиляция" in text
    # "_" экранирован (см. test_telegram_text_escapes_markdown_special_chars) —
    # иначе Telegram Markdown падает с 400 "can't parse entities" на голом "session_id="
    assert "Открыть диалог: http://localhost:8000/operator?session\\_id=abc" in text


def test_telegram_lead_text_drops_last_message_line_when_it_only_repeats_phone() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-11T16:15:09",
        payload={
            "name": "Иван",
            "phone": "+79991234567",
            "summary": "у меня воспаление что делать | Контакт: Иван +7 999 123-45-67",
            "reason": "medical_risk",
            "recent_messages": [
                {"role": "user", "text": "у меня воспаление что делать"},
                {"role": "assistant", "text": "..."},
                # разный формат телефона (пробелы/дефисы), но по цифрам совпадает с payload["phone"]
                {"role": "user", "text": "Иван +7 999 123-45-67"},
            ],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "Последнее сообщение: у меня воспаление что делать" in text
    assert "Последнее сообщение: Иван" not in text


def test_telegram_lead_text_omits_last_message_line_when_only_contact_given() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-12T14:26:50",
        payload={
            "name": "Иван",
            "phone": "+79261234567",
            "summary": "Иван хочет узнать о доступных услугах.",
            "reason": "commercial_interest",
            "recent_messages": [{"role": "user", "text": "Иван +79261234567"}],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "Последнее сообщение" not in text


def test_telegram_lead_text_omits_service_line_when_service_missing() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-12T14:26:50",
        payload={
            "name": "Иван",
            "phone": "+79261234567",
            "summary": "Иван хочет узнать о доступных услугах.",
            "reason": "commercial_interest",
            "recent_messages": [],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "Услуга:" not in text


def test_telegram_lead_text_marks_needs_operator_as_urgent() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-11T16:15:09",
        payload={
            "name": "Иван",
            "phone": "+79991234567",
            "summary": "у меня воспаление что делать",
            "reason": "medical_risk",
            "needs_operator": True,
            "recent_messages": [],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "🔴" in text
    assert "Срочная заявка" in text
    assert "Новая заявка" not in text


def test_telegram_lead_text_stays_regular_without_needs_operator() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-11T16:15:09",
        payload={
            "name": "Иван",
            "phone": "+79991234567",
            "summary": "сколько стоит эпиляция",
            "reason": "price_question",
            "needs_operator": False,
            "recent_messages": [],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "🔴" not in text
    assert "Срочная заявка" not in text
    assert "Новая заявка" in text


def test_telegram_unknown_service_lead_text_uses_labeled_name_and_phone() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-07-12T14:43:03",
        payload={
            "name": "Иван",
            "phone": "+79261234567",
            "reason": "unknown_service",
            "lead_trigger": "unknown_service",
            "unresolved_query": "татуаж",
            "recent_messages": [],
            "operator_url": "http://localhost:8000/operator",
        },
    )

    assert "Имя: Иван" in text
    assert "Телефон: +79261234567" in text


def test_telegram_unknown_service_lead_text_highlights_original_query() -> None:
    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={
            "name": "Иван",
            "phone": "+7999",
            "summary": "Пользователь спрашивал неподтверждённую услугу: «татуаж». Оставил контакт.",
            "reason": "unknown_service",
            "lead_trigger": "unknown_service",
            "unresolved_query": "татуаж",
            "recent_messages": [
                {"role": "user", "text": "татуаж"},
                {"role": "user", "text": "Иван +7999"},
            ],
            "operator_url": "http://localhost:8000/operator?session_id=abc",
        },
    )

    assert "Тип: Неизвестная услуга" in text
    assert "Запрос: татуаж" in text
    assert "Услуга в базе: не найдена" in text
    assert "Тип запроса:" not in text
    assert "Услуга: не указана" not in text


def test_telegram_text_escapes_unpaired_markdown_chars_in_dynamic_values() -> None:
    """regression: живой прогон на реальный Telegram API дал 400 "can't parse
    entities: Can't find end of the entity starting at byte offset 149" — голый "_"
    в "session_id=" внутри operator_url ломал парсинг Markdown целиком, и уведомление
    не доставлялось вообще (не просто криво выглядело — падало с ошибкой)."""

    text = _telegram_text(
        event_type="lead_created",
        company_name="Клиника",
        timestamp="2026-06-30T19:00:00",
        payload={
            "name": "Ива_н",
            "phone": "+7999",
            "summary": "Хочет *скидку* и уточнить `цену`",
            "operator_url": "http://localhost:8000/operator?session_id=bd647b64-8f87",
        },
    )

    # ровно одна НЕэкранированная пара "*" — заголовок "*Новая заявка*". Остальные
    # звёздочки (из summary) экранированы (\*), иначе непарный "*" тоже ломает Markdown.
    assert text.count("*Новая заявка*") == 1
    assert "\\*скидку\\*" in text
    assert "session\\_id" in text
    assert "Ива\\_н" in text
    assert "\\`цену\\`" in text


def test_webhook_payload_wraps_event_metadata() -> None:
    record = {
        "event_type": "operator_requested",
        "delivery_id": "delivery-1",
        "company_id": "rosh_demo",
        "timestamp": "2026-06-30T19:00:00",
        "payload": {"last_message": "оператор"},
    }

    payload = _webhook_payload(record)

    assert payload == {
        "event_type": "operator_requested",
        "delivery_id": "delivery-1",
        "company_id": "rosh_demo",
        "timestamp": "2026-06-30T19:00:00",
        "data": {"last_message": "оператор"},
    }


def test_webhook_send_adds_event_headers_with_stable_delivery_id(
    tmp_path: Path,
    resolver,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            calls.append({"url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(delivery_module.httpx, "AsyncClient", FakeAsyncClient)
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
    )
    record = {
        "timestamp": "2026-06-30T19:00:00",
        "delivery_id": "stable-delivery-id",
        "event_type": "operator_requested",
        "company_id": "rosh_demo",
        "session_id": "session-1",
        "destination_type": "webhook",
        "target": "https://example.test/webhook",
        "payload": {"last_message": "оператор"},
    }

    async def run_send_twice() -> None:
        await service._send(record)
        await service._send(record)

    import anyio

    anyio.run(run_send_twice)

    assert calls[0]["headers"]["X-Widget-Event"] == "operator_requested"
    assert calls[0]["headers"]["X-Delivery-ID"] == "stable-delivery-id"
    assert calls[1]["headers"]["X-Delivery-ID"] == "stable-delivery-id"
    assert calls[0]["json"]["event_type"] == "operator_requested"
    assert calls[0]["json"]["data"] == {"last_message": "оператор"}


def test_retry_due_retries_failed_record_and_marks_sent(tmp_path: Path, resolver, monkeypatch) -> None:
    outbox_file = tmp_path / "delivery_outbox.jsonl"
    service = DeliveryService(
        outbox_file=outbox_file,
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )
    failed_record = {
        "timestamp": "2026-06-30T19:00:00",
        "delivery_id": "delivery-retry-1",
        "event_type": "lead_created",
        "company_id": "rosh_demo",
        "session_id": "session-1",
        "destination_type": "telegram",
        "status": "failed",
        "attempts": 1,
        "next_attempt_at": "2020-01-01T00:00:00",
        "target": "chat",
        "payload": {"summary": "лид"},
        "last_error": "ConnectError",
        "response_status": None,
    }

    async def fake_send(record: dict[str, Any]) -> int:
        assert record["delivery_id"] == "delivery-retry-1"
        return 200

    async def run_retry() -> dict[str, Any]:
        await service._append_record(failed_record)
        return await service.retry_due()

    import anyio

    monkeypatch.setattr(service, "_send", fake_send)
    result = anyio.run(run_retry)
    records = read_jsonl(outbox_file)

    assert result == {"attempted": 1, "sent": 1, "failed": 0, "dead": 0}
    assert records[-1]["delivery_id"] == "delivery-retry-1"
    assert records[-1]["status"] == "sent"
    assert records[-1]["attempts"] == 2


def test_run_retry_loop_cancels_cleanly(tmp_path: Path, resolver) -> None:
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
    )

    async def run_loop() -> None:
        task = asyncio.create_task(service.run_retry_loop(60))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("retry loop task did not propagate cancellation")

    import anyio

    anyio.run(run_loop)


def test_run_retry_loop_continues_after_retry_error(tmp_path: Path, resolver, monkeypatch) -> None:
    service = DeliveryService(
        outbox_file=tmp_path / "delivery_outbox.jsonl",
        knowledge_base_resolver=resolver,
    )
    calls = 0
    second_call = asyncio.Event()

    async def fake_retry_due(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        second_call.set()
        return {"attempted": 0, "sent": 0, "failed": 0, "dead": 0}

    async def run_loop() -> None:
        monkeypatch.setattr(service, "retry_due", fake_retry_due)
        task = asyncio.create_task(service.run_retry_loop(0))
        await asyncio.wait_for(second_call.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    import anyio

    anyio.run(run_loop)
    assert calls >= 2
