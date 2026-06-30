"""интеграционные проверки chat message flow."""


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
