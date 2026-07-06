"""проверки API операторской панели."""


OPERATOR_HEADERS = {"x-operator-token": "demo-operator-token"}


def _send_message(test_client, message: str) -> dict:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": message},
    )
    assert response.status_code == 200
    return response.json()


def test_operator_sessions_queue_scope_keeps_only_operator_queue(test_client) -> None:
    ai_payload = _send_message(test_client, "привет")
    waiting_payload = _send_message(test_client, "у меня воспаление что делать")

    response = test_client.get("/api/operator/sessions", headers=OPERATOR_HEADERS)
    payload = response.json()
    session_ids = {item["session_id"] for item in payload}

    assert response.status_code == 200
    assert ai_payload["session_id"] not in session_ids
    assert waiting_payload["session_id"] in session_ids
    assert all(item["status"] in {"WAITING_OPERATOR", "HUMAN_ACTIVE"} for item in payload)


def test_operator_sessions_all_scope_includes_ai_active(test_client) -> None:
    ai_payload = _send_message(test_client, "привет")
    waiting_payload = _send_message(test_client, "у меня воспаление что делать")

    response = test_client.get(
        "/api/operator/sessions?scope=all",
        headers=OPERATOR_HEADERS,
    )
    payload = response.json()
    sessions_by_id = {item["session_id"]: item for item in payload}

    assert response.status_code == 200
    assert sessions_by_id[ai_payload["session_id"]]["status"] == "AI_ACTIVE"
    assert sessions_by_id[waiting_payload["session_id"]]["status"] == "WAITING_OPERATOR"


def test_operator_sessions_scope_requires_operator_token(test_client) -> None:
    queue_response = test_client.get("/api/operator/sessions")
    all_response = test_client.get("/api/operator/sessions?scope=all")

    assert queue_response.status_code == 403
    assert all_response.status_code == 403
