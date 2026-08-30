"""проверки API операторской панели."""

from datetime import date, datetime, timedelta

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


def test_analytics_chats_requires_token(test_client) -> None:
    response = test_client.get("/api/analytics/chats")
    assert response.status_code == 403


def test_analytics_chats_lists_live_session_and_supports_scope_filter(test_client) -> None:
    """Вкладка "Чаты" (TSK-05): живая сессия появляется в списке сразу, без ожидания
    архивации, и фильтры отсекают лишнее."""

    bot_only = _send_message(test_client, "привет")
    operator_payload = _request_operator_handoff(test_client)

    all_response = test_client.get("/api/analytics/chats?company_id=rosh_demo", headers=OPERATOR_HEADERS)
    assert all_response.status_code == 200
    all_ids = {item["session_id"] for item in all_response.json()["conversations"]}
    assert bot_only["session_id"] in all_ids
    assert operator_payload["session_id"] in all_ids

    operator_scope = test_client.get(
        "/api/analytics/chats?company_id=rosh_demo&scope=operator", headers=OPERATOR_HEADERS
    )
    operator_ids = {item["session_id"] for item in operator_scope.json()["conversations"]}
    assert operator_payload["session_id"] in operator_ids
    assert bot_only["session_id"] not in operator_ids

    bot_only_scope = test_client.get(
        "/api/analytics/chats?company_id=rosh_demo&scope=bot_only", headers=OPERATOR_HEADERS
    )
    bot_only_ids = {item["session_id"] for item in bot_only_scope.json()["conversations"]}
    assert bot_only["session_id"] in bot_only_ids
    assert operator_payload["session_id"] not in bot_only_ids


def test_analytics_chat_detail_returns_full_transcript_and_404s_for_unknown(test_client) -> None:
    payload = _send_message(test_client, "привет, сколько стоит?")
    session_id = payload["session_id"]

    response = test_client.get(f"/api/analytics/chats/{session_id}", headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    detail = response.json()
    assert detail["source"] == "live"
    assert detail["messages"][0]["text"] == "привет, сколько стоит?"

    missing = test_client.get("/api/analytics/chats/does-not-exist", headers=OPERATOR_HEADERS)
    assert missing.status_code == 404


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


def test_analytics_page_hides_company_selector_and_never_mentions_rosh_test(test_client) -> None:
    """2026-08-29: /analytics — для клиента (если/когда отдадим доступ), он не должен ни
    видеть переключатель компаний, ни узнать, что вообще существует rosh_test (наш
    изолированный company_id для тестового трафика, см. /backstage)."""

    response = test_client.get("/analytics?token=demo-operator-token")
    assert response.status_code == 200
    assert "rosh_test" not in response.text
    assert 'style="display:none"' in response.text


def test_backstage_page_requires_auth_same_as_analytics(test_client) -> None:
    response = test_client.get("/backstage", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith("/login")


def test_backstage_page_shows_selector_defaulting_to_rosh_test(test_client) -> None:
    """Тот же render_analytics_panel, что и /analytics (не копия кода) — просто по умолчанию
    смотрит на rosh_test и показывает дропдаун, только для внутреннего использования."""

    response = test_client.get("/backstage?token=demo-operator-token")
    assert response.status_code == 200
    assert "rosh_test" in response.text
    assert "rosh_import_demo" in response.text
    assert "Аналитика" in response.text


def test_backstage_dropdown_has_no_duplicate_options(test_client) -> None:
    """Живой баг (найден в браузере, 2026-08-29): default_company_id="rosh_test" совпадал с
    захардкоженным вторым вариантом — на экране было два одинаковых "rosh_test" и ни одного
    "rosh_import_demo". Считаем реальные <option> в дропдауне, не просто ищем подстроку."""

    response = test_client.get("/backstage?token=demo-operator-token")
    assert response.text.count('<option value="rosh_test"') == 1
    assert response.text.count('<option value="rosh_import_demo"') == 1


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
        "company_id", "days", "range_label", "summary", "operators", "leads_by_month", "leads_by_reason",
        "top_services", "funnel", "unanswered_trend", "intent_breakdown", "objection_breakdown",
        "top_unanswered_questions", "top_answered_questions", "activity_by_hour", "activity_by_weekday",
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


# ── кастомный диапазон дат (2026-08-30) ──


def test_analytics_dashboard_custom_range_filters_leads(test_client) -> None:
    from app.utils.jsonl import append_jsonl

    in_range_lead = {
        "timestamp": "2026-08-15T10:00:00", "company_id": "rosh_demo", "session_id": "in-range",
        "name": "Иван", "phone": "+79990000000", "summary": "", "reason": "booking", "service_id": None,
    }
    out_of_range_lead = {
        "timestamp": "2026-07-01T10:00:00", "company_id": "rosh_demo", "session_id": "out-of-range",
        "name": "Пётр", "phone": "+79990000001", "summary": "", "reason": "booking", "service_id": None,
    }
    append_jsonl(test_client.app.state.settings.leads_file, in_range_lead)
    append_jsonl(test_client.app.state.settings.leads_file, out_of_range_lead)

    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&start_date=2026-08-01&end_date=2026-08-31",
        headers=OPERATOR_HEADERS,
    )
    result = response.json()

    assert response.status_code == 200
    assert sum(entry["count"] for entry in result["leads_by_reason"]) == 1
    assert result["days"] is None
    assert result["range_label"] == "01.08.2026–31.08.2026"
    assert result["period_comparison"] is None


def test_analytics_dashboard_custom_range_requires_both_dates(test_client) -> None:
    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&start_date=2026-08-01", headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422


def test_analytics_dashboard_custom_range_rejects_end_before_start(test_client) -> None:
    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&start_date=2026-08-31&end_date=2026-08-01",
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422


def test_analytics_dashboard_custom_range_rejects_too_long(test_client) -> None:
    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&start_date=2020-01-01&end_date=2026-08-30",
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422


def test_analytics_dashboard_custom_range_funnel_clamp_uses_midnight_not_time_max(test_client) -> None:
    """Живой баг (код-ревью, 2026-08-30): клэмп воронки для широкого кастомного периода
    раньше наследовал time.max (23:59:59.999999) от end — граничный день почти целиком
    выпадал из подсчёта. Событие рано утром граничного дня должно попасть в воронку."""

    from app.utils.jsonl import append_jsonl

    end_date = date(2026, 8, 30)
    start_date = end_date - timedelta(days=89)  # шире 55 дней — клэмп реально сработает
    clamp_date = end_date - timedelta(days=54)  # ровно граничный день после клэмпа
    early_morning = datetime.combine(clamp_date, datetime.min.time()) + timedelta(minutes=30)

    impression = {
        "timestamp": early_morning.isoformat(), "company_id": "rosh_demo", "session_id": "s-clamp",
        "event_type": "widget_impression", "message": None, "metadata": {},
    }
    append_jsonl(test_client.app.state.settings.analytics_file, impression)

    response = test_client.get(
        f"/api/analytics/dashboard?company_id=rosh_demo&start_date={start_date.isoformat()}"
        f"&end_date={end_date.isoformat()}",
        headers=OPERATOR_HEADERS,
    )
    stages = {stage["label"]: stage["count"] for stage in response.json()["funnel"]["stages"]}

    assert stages["Виджет загружен"] == 1


def test_analytics_dashboard_preset_days_unaffected_by_custom_range_code(test_client) -> None:
    """Пресетный путь (days=N без start_date/end_date) должен вести себя ровно как раньше —
    days число, range_label осмысленный, period_comparison присутствует."""

    response = test_client.get(
        "/api/analytics/dashboard?company_id=rosh_demo&days=30", headers=OPERATOR_HEADERS
    )
    result = response.json()

    assert response.status_code == 200
    assert result["days"] == 30
    assert result["range_label"] == "30 дн."
    assert result["period_comparison"] is not None


# ── лиды, "уровень 0" (2026-08-30) — таблица без персональных данных ──


def test_analytics_leads_requires_token(test_client) -> None:
    response = test_client.get("/api/analytics/leads?company_id=rosh_demo")
    assert response.status_code == 403


def test_analytics_leads_returns_no_pii_over_the_wire(test_client) -> None:
    from app.utils.jsonl import append_jsonl

    lead = {
        "timestamp": datetime.utcnow().isoformat(), "company_id": "rosh_demo", "session_id": "s-1",
        "name": "Секретное Имя", "phone": "+79991234567", "summary": "секретная сводка",
        "reason": "booking", "service_id": None,
    }
    append_jsonl(test_client.app.state.settings.leads_file, lead)

    response = test_client.get("/api/analytics/leads?company_id=rosh_demo", headers=OPERATOR_HEADERS)
    raw_body = response.text

    assert response.status_code == 200
    assert "Секретное Имя" not in raw_body
    assert "+79991234567" not in raw_body
    assert "секретная сводка" not in raw_body
    result = response.json()
    assert len(result["leads"]) == 1
    forbidden = {"name", "phone", "summary", "recent_messages"}
    assert forbidden.isdisjoint(result["leads"][0].keys())


def test_analytics_leads_resolves_service_name(test_client) -> None:
    from app.utils.jsonl import append_jsonl

    lead = {
        "timestamp": datetime.utcnow().isoformat(), "company_id": "rosh_demo", "session_id": "s-1",
        "name": "Иван", "phone": "+79990000000", "summary": "", "reason": "booking",
        "service_id": "chistka_lica",
    }
    append_jsonl(test_client.app.state.settings.leads_file, lead)

    response = test_client.get("/api/analytics/leads?company_id=rosh_demo", headers=OPERATOR_HEADERS)
    result = response.json()

    assert result["leads"][0]["service_id"] == "chistka_lica"
    assert isinstance(result["leads"][0]["service_name"], str)


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
