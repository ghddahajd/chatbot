"""проверки generic delivery outbox."""

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
    assert "Открыть диалог: http://localhost:8000/operator?session_id=abc" in text


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
