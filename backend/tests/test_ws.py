"""вебсокет-канал живого чата (/ws/chat/{session_id}) — тот путь, которым виджет реально
шлёт сообщения клиента, пока статус HUMAN_ACTIVE и соединение открыто (см. widget.js
sendText). Живой баг (ручное тестирование пользователем, 2026-08-28): лид-детект для
HUMAN_ACTIVE был добавлен только в REST-ветку handle_message — через этот вебсокет
сообщения вообще не доходили до неё, контакт клиента терялся молча."""

from app.models import SessionStatus

from .test_chat_service import _FakeTelegramBridge


def test_ws_chat_captures_lead_when_client_gives_contact_while_human_active(test_client) -> None:
    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
    ).json()["session_id"]
    test_client.app.state.session_store._sessions[session_id].status = SessionStatus.HUMAN_ACTIVE

    with test_client.websocket_connect(f"/ws/chat/{session_id}?company_id=rosh_demo") as ws:
        ws.send_text("Леха, 89991234567")

    stored_session = test_client.app.state.session_store._sessions[session_id]
    assert stored_session.lead_requested is True
    assert stored_session.status == SessionStatus.HUMAN_ACTIVE

    assert fake_bridge.forwarded == [(session_id, "Леха, 89991234567")]
    assert fake_bridge.queue_cards == []
    assert len(fake_bridge.client_cards) == 1
    assert "Леха" in fake_bridge.client_cards[0]

    session_response = test_client.get(f"/api/chat/session/{session_id}")
    messages = session_response.json()["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["text"] == "Леха, 89991234567"


def test_ws_chat_does_not_duplicate_lead_on_repeated_contact(test_client) -> None:
    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
    ).json()["session_id"]
    test_client.app.state.session_store._sessions[session_id].status = SessionStatus.HUMAN_ACTIVE

    with test_client.websocket_connect(f"/ws/chat/{session_id}?company_id=rosh_demo") as ws:
        ws.send_text("Леха, 89991234567")
        ws.send_text("мой номер 89991234567")

    assert len(fake_bridge.client_cards) == 1


def test_ws_chat_without_contact_does_not_create_lead(test_client) -> None:
    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
    ).json()["session_id"]
    test_client.app.state.session_store._sessions[session_id].status = SessionStatus.HUMAN_ACTIVE

    with test_client.websocket_connect(f"/ws/chat/{session_id}?company_id=rosh_demo") as ws:
        ws.send_text("когда откроетесь?")

    stored_session = test_client.app.state.session_store._sessions[session_id]
    assert stored_session.lead_requested is False
    assert fake_bridge.client_cards == []
