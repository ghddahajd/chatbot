"""проверки API операторской панели."""


OPERATOR_HEADERS = {"x-operator-token": "demo-operator-token"}


def _send_message(test_client, message: str) -> dict:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": message},
    )
    assert response.status_code == 200
    return response.json()


def _request_operator_handoff(test_client) -> dict:
    first_payload = _send_message(test_client, "оператор")
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "да, менеджера",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "transfer_operator"
    return payload


def test_handoff_message_marked_with_kind_for_panel(test_client) -> None:
    """P0-2 regression: сообщение о передаче оператору помечается kind='handoff'
    структурно, а не распознаётся панелью по тексту (тексты handoff у клиентов разные:
    'менеджеру'/'мастеру'/'вопрос менеджеру'), поэтому match по строке молча ломался."""
    payload = _request_operator_handoff(test_client)

    session = test_client.get(
        f"/api/operator/sessions/{payload['session_id']}", headers=OPERATOR_HEADERS
    ).json()
    assistant_messages = [m for m in session["messages"] if m["role"] == "assistant"]
    assert assistant_messages
    assert assistant_messages[-1]["kind"] == "handoff"


def test_regular_answer_has_no_handoff_kind(test_client) -> None:
    """P0-2 guard: обычный ответ НЕ помечается handoff, иначе панель нарисует его
    системным разделителем."""
    payload = _send_message(test_client, "привет")

    session = test_client.get(
        f"/api/operator/sessions/{payload['session_id']}", headers=OPERATOR_HEADERS
    ).json()
    assistant_messages = [m for m in session["messages"] if m["role"] == "assistant"]
    assert assistant_messages
    assert all(m.get("kind") != "handoff" for m in assistant_messages)


def test_operator_sessions_queue_scope_keeps_only_operator_queue(test_client) -> None:
    ai_payload = _send_message(test_client, "привет")
    waiting_payload = _request_operator_handoff(test_client)

    response = test_client.get("/api/operator/sessions", headers=OPERATOR_HEADERS)
    payload = response.json()
    session_ids = {item["session_id"] for item in payload}

    assert response.status_code == 200
    assert ai_payload["session_id"] not in session_ids
    assert waiting_payload["session_id"] in session_ids
    assert all(item["status"] in {"WAITING_OPERATOR", "HUMAN_ACTIVE"} for item in payload)


def test_operator_sessions_all_scope_includes_ai_active(test_client) -> None:
    ai_payload = _send_message(test_client, "привет")
    waiting_payload = _request_operator_handoff(test_client)

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
