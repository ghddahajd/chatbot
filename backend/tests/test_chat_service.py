"""интеграционные проверки chat message flow."""

import json


def test_contact_prompt_stays_ai_active_and_can_be_cancelled(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Хочу оставить телефон"},
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "ask_contact"
    assert first_payload["status"] == "AI_ACTIVE"
    assert "телефон" in first_payload["answer"].lower()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "нет",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert second_payload["status"] == "AI_ACTIVE"
    assert "контакт не оставляем" in second_payload["answer"].lower()


def test_booking_contact_enqueues_booking_event(test_client, monkeypatch) -> None:
    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    assert events[-1]["event_type"] == "booking_created"
    assert events[-1]["company_id"] == "rosh_demo"
    assert events[-1]["session_id"] == first_payload["session_id"]


def test_successful_booking_clears_pending_and_does_not_duplicate_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    second_payload = second_response.json()
    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True

    leads_file = managed_env["temp_dir"] / "leads.jsonl"
    leads_before = leads_file.read_text(encoding="utf-8").splitlines()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "+7 999 123-45-67",
        },
    )
    third_payload = third_response.json()
    leads_after = leads_file.read_text(encoding="utf-8").splitlines()

    assert third_response.status_code == 200
    assert third_payload["lead_created"] is False
    assert len(leads_after) == len(leads_before)


def test_transfer_operator_enqueues_operator_requested_event(test_client, monkeypatch) -> None:
    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "transfer_operator"
    assert events[-1]["event_type"] == "operator_requested"
    assert events[-1]["payload"]["last_message"] == "у меня воспаление что делать"
    operator_url = events[-1]["payload"]["operator_url"]
    assert "/operator?token=" in operator_url
    assert "demo-operator-token" in operator_url


def test_operator_second_request_transfers_after_soft_offer(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    )
    first_payload = first_response.json()
    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Да, оператора",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "transfer_operator"
    assert second_payload["status"] == "WAITING_OPERATOR"


def test_pending_contact_accepts_messy_phone_and_name(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "89999229333 леха",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "ask_contact"
    assert second_payload["status"] == "AI_ACTIVE"
    assert second_payload["lead_created"] is True


def test_pending_contact_lead_summary_keeps_prior_user_request(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу татуаж"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert "татуаж" in lead["summary"].lower()
    assert "Контакт: Иван +79991234567" in lead["summary"]


def test_direct_phone_after_price_question_keeps_prior_reason_and_service(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "сколько стоит чистка лица"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["reason"] == "price_question"
    assert lead["service_id"] == "facial_cleansing"


def test_contact_request_with_phone_in_same_message_creates_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу оставить телефон\n89999229333 леха",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ask_contact"
    assert payload["status"] == "AI_ACTIVE"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["name"] == "Леха"
    assert lead["phone"] == "+79999229333"


def test_unknown_booking_service_with_phone_does_not_create_lead(test_client) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на шиномонтаж\n89999229333 леха",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "clarify"
    assert payload["lead_created"] is False


def test_partial_phone_does_not_go_to_llm(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "8999922933 леха",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert second_payload["lead_created"] is False
    assert "номер неполный" in second_payload["answer"].lower()


def test_explicit_service_beats_previous_service_context(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "сколько стоит Консультация дерматолога",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "Консультация дерматолога" in second_payload["answer"]


def test_new_question_breaks_out_of_pending_booking_contact(test_client) -> None:
    """regression: до фикса любое сообщение после 'хочу записаться' (без телефона)
    навсегда перехватывалось запросом телефона, а extract_name вытаскивал случайное
    слово вопроса как "имя" (например "Какие, напишите телефон..." из "какие врачи
    у вас есть?"). Новый вопрос должен реально отвечаться, не зацикливать."""

    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу записаться на чистку лица"},
    )
    session_id = first_response.json()["session_id"]

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "какие врачи у вас есть?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert "Какие" not in second_payload["answer"]
    assert "напишите, пожалуйста, телефон — передам заявку" not in second_payload["answer"]
