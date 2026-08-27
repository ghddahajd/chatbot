"""проверки API операторской панели."""

from datetime import datetime, timedelta

import anyio

from .test_telegram_bridge import FakeAsyncClient, _reset_fake_client


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


def test_analytics_operators_requires_token(test_client) -> None:
    response = test_client.get("/api/analytics/operators")
    assert response.status_code == 403


def test_analytics_operators_reflects_real_telegram_claim_and_close(test_client, monkeypatch) -> None:
    """Интеграционный прогон через реально подключённый (не подменённый) app.state.
    telegram_bridge_service — проверяет всю цепочку main.py wiring, не только изолированную
    логику analytics.py/telegram_bridge.py по отдельности. httpx.AsyncClient подменён (см.
    test_telegram_bridge.FakeAsyncClient) — иначе с пустым TELEGRAM_BOT_TOKEN из тестового
    окружения это бьёт живьём в api.telegram.org и просто получает 404 на каждый вызов."""

    _reset_fake_client(monkeypatch)
    FakeAsyncClient.responses["createForumTopic"] = {"ok": True, "result": {"message_thread_id": 42}}

    payload = _request_operator_handoff(test_client)
    session_id = payload["session_id"]
    bridge = test_client.app.state.telegram_bridge_service

    async def claim_and_close() -> None:
        await bridge._handle_callback_query(
            {
                "id": "cb1",
                "data": f"claim:{session_id}",
                "from": {"username": "masha"},
                "message": {},
            }
        )
        session = await test_client.app.state.session_store.get(session_id)
        await bridge._close_session_from_topic(session_id, session.telegram_topic_id)

    anyio.run(claim_and_close)

    response = test_client.get(
        "/api/analytics/operators?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    result = response.json()

    assert response.status_code == 200
    assert result["operators"]["masha"]["claimed"] == 1
    assert result["operators"]["masha"]["closed"] == 1


def test_analytics_dashboard_requires_token(test_client) -> None:
    response = test_client.get("/api/analytics/dashboard")
    assert response.status_code == 403


def test_analytics_page_redirects_to_login_when_unauthenticated(test_client) -> None:
    """2026-08-27: раньше без токена /analytics отдавал голый 403, теперь уводит на форму
    логина — токену больше не обязательно светиться в URL."""

    response = test_client.get("/analytics", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith("/login")


def test_analytics_page_renders_with_token(test_client) -> None:
    response = test_client.get("/analytics?token=demo-operator-token")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Аналитика" in response.text


def test_login_page_renders(test_client) -> None:
    response = test_client.get("/login")
    assert response.status_code == 200
    assert "Пароль" in response.text


def test_login_wrong_password_shows_error_and_no_cookie(test_client) -> None:
    response = test_client.post("/login", data={"password": "wrong"})
    assert response.status_code == 401
    assert "Неверный пароль" in response.text
    assert "operator_token" not in response.cookies


def test_login_correct_password_sets_cookie_and_redirects(test_client) -> None:
    response = test_client.post(
        "/login", data={"password": "demo-operator-token"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/analytics"
    assert response.cookies.get("operator_token") == "demo-operator-token"


def test_analytics_page_works_via_cookie_without_url_token(test_client) -> None:
    test_client.post("/login", data={"password": "demo-operator-token"})
    response = test_client.get("/analytics")

    assert response.status_code == 200
    assert "Аналитика" in response.text


def test_logout_clears_cookie_and_redirects_to_login(test_client) -> None:
    test_client.post("/login", data={"password": "demo-operator-token"})
    response = test_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert test_client.get("/analytics", follow_redirects=False).status_code in (302, 307)


def test_analytics_dashboard_resolves_service_name_and_shape(test_client) -> None:
    first_payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    ).json()
    test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )

    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    result = response.json()

    assert response.status_code == 200
    assert set(result.keys()) == {
        "company_id", "days", "summary", "operators", "leads_by_month", "leads_by_reason", "top_services",
        "funnel", "unanswered_trend", "activity_by_hour", "activity_by_weekday",
        "queue_wait", "period_comparison",
    }
    assert result["summary"]["leads"]["total"] >= 1
    assert len(result["leads_by_month"]) == 6
    assert len(result["activity_by_hour"]) == 24
    assert len(result["activity_by_weekday"]) == 7
    assert result["leads_by_month"][-1]["month"] == datetime.utcnow().strftime("%Y-%m")
    assert [stage["label"] for stage in result["funnel"]["stages"]] == [
        "Виджет загружен", "Чат открыт", "Есть переписка", "Стал лидом",
    ]


def test_analytics_dashboard_days_filter_excludes_old_leads(test_client) -> None:
    """Общий date-range фильтр дашборда (2026-08-27) — days=7 не должен видеть лид старше
    недели, days=90 должен."""

    from app.utils.jsonl import append_jsonl

    old_lead = {
        "timestamp": (datetime.utcnow() - timedelta(days=40)).isoformat(),
        "company_id": "rosh_demo",
        "session_id": "old-session",
        "name": "Старый Лид",
        "phone": "+79990000000",
        "summary": "",
        "reason": "booking",
        "service_id": None,
    }
    append_jsonl(test_client.app.state.settings.leads_file, old_lead)

    narrow = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&days=7", headers=OPERATOR_HEADERS
    ).json()
    wide = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&days=90", headers=OPERATOR_HEADERS
    ).json()

    assert sum(entry["count"] for entry in narrow["leads_by_reason"]) == 0
    assert sum(entry["count"] for entry in wide["leads_by_reason"]) == 1


def test_track_impression_writes_analytics_event(test_client) -> None:
    response = test_client.post("/api/analytics/track/impression", json={"company_id": "rosh_demo"})
    assert response.status_code == 200

    dashboard = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo", headers=OPERATOR_HEADERS
    ).json()
    stages = {stage["label"]: stage["count"] for stage in dashboard["funnel"]["stages"]}
    assert stages["Виджет загружен"] == 1


def test_track_chat_opened_writes_analytics_event(test_client) -> None:
    response = test_client.post(
        "/api/analytics/track/chat-opened", json={"company_id": "rosh_demo", "session_id": "s-1"}
    )
    assert response.status_code == 200

    dashboard = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo", headers=OPERATOR_HEADERS
    ).json()
    stages = {stage["label"]: stage["count"] for stage in dashboard["funnel"]["stages"]}
    assert stages["Чат открыт"] == 1
