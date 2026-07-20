"""интеграционные проверки chat message flow."""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient


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


def test_booking_prompt_can_be_cancelled_with_common_typo(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу заптсаьбся на чистку лица"},
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"
    assert "телефон" in first_payload["answer"].lower()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "омтена",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "заявку не оформляем" in second_payload["answer"].lower()
    assert second_payload["lead_created"] is False


def test_booking_prompt_accepts_service_name_as_next_step(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "запишите меня"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "на чистку лица",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "телефон" in second_payload["answer"].lower()
    assert "такой услуги" not in second_payload["answer"].lower()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    third_payload = third_response.json()

    assert third_response.status_code == 200
    assert third_payload["lead_created"] is True


def test_contact_prompt_still_allows_new_price_question(test_client) -> None:
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
            "message": "а сколько стоит чистка лица",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "стоимость" in second_payload["answer"].lower() or "₽" in second_payload["answer"]


def test_cancel_after_price_booking_compound_does_not_become_unknown_service(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "сколько стоит чистка лица и можно записаться",
        },
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "answer"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "отмена",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "ничего не оформляем" in second_payload["answer"].lower()
    assert "такой услуги" not in second_payload["answer"].lower()
    assert second_payload["lead_created"] is False


def test_price_followup_after_variant_reuses_last_variant(managed_env) -> None:
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)

    from app.main import app

    with TestClient(app) as test_client:
        first_response = test_client.post(
            "/api/chat/message",
            json={"company_id": "rosh_import_demo", "session_id": None, "message": "лазерная эпиляция"},
        )
        first_payload = first_response.json()

        second_response = test_client.post(
            "/api/chat/message",
            json={
                "company_id": "rosh_import_demo",
                "session_id": first_payload["session_id"],
                "message": "а на ногах?",
            },
        )
        second_payload = second_response.json()

        third_response = test_client.post(
            "/api/chat/message",
            json={
                "company_id": "rosh_import_demo",
                "session_id": first_payload["session_id"],
                "message": "а по деньгам?",
            },
        )
        third_payload = third_response.json()

    assert second_response.status_code == 200
    assert third_response.status_code == 200
    assert "ноги полностью" in second_payload["answer"].lower()
    assert "21 600" in second_payload["answer"]
    assert "ноги полностью" in third_payload["answer"].lower()
    assert "21 600" in third_payload["answer"]
    assert "от 1 800 до 24 000" not in third_payload["answer"]


def test_chat_rate_limit_blocks_single_ip_but_not_other_ips(managed_env, monkeypatch) -> None:
    monkeypatch.setenv("CHAT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        headers = {"X-Forwarded-For": "203.0.113.10"}
        first_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
            headers=headers,
        )
        second_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": first_response.json()["session_id"], "message": "услуги"},
            headers=headers,
        )
        limited_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": first_response.json()["session_id"], "message": "цены"},
            headers=headers,
        )
        other_ip_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
            headers={"X-Forwarded-For": "203.0.113.11"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert limited_response.status_code == 429
    assert other_ip_response.status_code == 200
    get_settings.cache_clear()


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


def test_regulated_question_soft_offers_without_operator_event(test_client, monkeypatch) -> None:
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
    assert payload["action"] == "clarify"
    assert payload["status"] == "AI_ACTIVE"
    assert "103" in payload["answer"]
    assert [action["label"] for action in payload["quick_actions"]] == [
        "Оставить телефон",
        "Подключить менеджера",
    ]
    assert events == []


def test_regulated_soft_contact_creates_flagged_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
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
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert second_response.status_code == 200
    assert second_payload["action"] == "ask_contact"
    assert second_payload["lead_created"] is True
    assert second_payload["status"] == "AI_ACTIVE"
    assert lead["reason"] == "medical_risk"
    assert lead["needs_operator"] is True
    assert lead["lead_trigger"] == "regulated_advice"


def test_regulated_lead_mode_normal_creates_default_lead(test_client, managed_env) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").rstrip()
        + "\n  regulated_lead_mode: normal\n",
        encoding="utf-8",
    )
    test_client.app.state.knowledge_base_resolver._cache.clear()

    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
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
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert second_response.status_code == 200
    assert lead["reason"] == "commercial_interest"
    assert lead["needs_operator"] is False
    assert lead["lead_trigger"] == "ask_contact"


def test_regulated_instant_override_enqueues_operator_requested_event(
    test_client,
    managed_env,
    monkeypatch,
) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").rstrip()
        + "\n  regulated_escalation: instant\n",
        encoding="utf-8",
    )
    test_client.app.state.knowledge_base_resolver._cache.clear()
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
    assert payload["status"] == "WAITING_OPERATOR"
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
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert lead["unresolved_query"] == "хочу татуаж"
    assert "татуаж" in lead["summary"].lower()
    assert "неподтверждённую услугу" in lead["summary"]


def test_unknown_service_booking_phone_creates_marked_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу записаться на биоиоиои"},
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
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "биоиоиои" in lead["unresolved_query"]
    assert "биоиоиои" in lead["summary"]


def test_regular_contact_lead_is_not_marked_unknown_service(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Иван +79991234567 хочу узнать"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["lead_trigger"] == "ask_contact"
    assert lead["reason"] == "commercial_interest"
    assert lead["unresolved_query"] == ""


def test_offdomain_phone_same_message_creates_unknown_service_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Леха, 89990000000, нужна уборка после ремонта",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ask_contact"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["name"] == "Леха"
    assert lead["phone"] == "+79990000000"
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "уборка после ремонта" in lead["unresolved_query"].lower()
    assert "неподтверждённую услугу" in lead["summary"]


def test_real_service_phone_same_message_stays_regular_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Иван 89990000000 хочу на чистку лица",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] == "facial_cleansing"
    assert lead["lead_trigger"] == "ask_contact"
    assert lead["reason"] == "commercial_interest"
    assert lead["unresolved_query"] == ""


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


def test_unknown_booking_service_with_phone_creates_marked_lead(test_client, managed_env) -> None:
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
    assert payload["action"] == "ask_contact"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "шиномонтаж" in lead["unresolved_query"].lower()


def test_unknown_service_booking_prompt_keeps_unresolved_query_in_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "татуаж делаете?"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "ну хочу записаться",
        },
    )
    second_payload = second_response.json()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван 89990000000",
        },
    )
    third_payload = third_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert third_response.status_code == 200
    assert third_payload["action"] == "ask_contact"
    assert third_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert lead["unresolved_query"] == "татуаж делаете?"
    assert "татуаж" in lead["summary"].lower()


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


def test_context_frame_keeps_service_for_short_price_followup(test_client) -> None:
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
            "message": "а всё-таки почём?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "Консультация косметолога" in second_payload["answer"]


def test_context_frame_keeps_doctor_topic_for_followup(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "кто у вас гинеколог?"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "а дерматолог кто?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] in {"answer", "clarify"}
    assert "врач" in second_payload["answer"].lower() or "специалист" in second_payload["answer"].lower()


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
