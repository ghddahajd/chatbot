"""GET/POST /api/settings/company — "Настройки"-таб (TSK-06, 2026-08-29). Персистентность
через реальный docker-entrypoint.sh-стиль rm -rf уже проверена на уровне
config_overrides.py (test_config_overrides.py) — тут проверяем HTTP-слой: auth, валидацию,
и что сохранение реально доезжает до следующего же GET без рестарта (сброс кэша resolver'а)."""

OPERATOR_HEADERS = {"x-operator-token": "demo-operator-token"}

_VALID_PAYLOAD = {
    "phone": "+7 999 000-00-00",
    "address": "Новый адрес",
    "telegram_url": "https://t.me/example",
    "website_url": "https://example.com",
    "working_hours_schedule": {
        "mon": {"open": "09:00", "close": "20:00"},
        "tue": {"open": "09:00", "close": "20:00"},
        "wed": {"open": "09:00", "close": "20:00"},
        "thu": {"open": "09:00", "close": "20:00"},
        "fri": {"open": "09:00", "close": "20:00"},
        "sat": None,
        "sun": None,
    },
    "widget": {
        "primary_color": "#111111",
        "button_color": "#222222",
        "header_title": "Новый заголовок",
        "header_subtitle": "Новый подзаголовок",
        "position": "bottom-left",
        "avatar_emoji": "🩺",
    },
    "facts": {
        "oms": True,
        "dms": False,
        "ambulance_brings": True,
        "sells_products": False,
        "discloses_doctor_schedule": True,
    },
    "doctors": [],
}


def test_get_settings_requires_operator_token(test_client) -> None:
    response = test_client.get("/api/settings/company?company_id=rosh_demo")
    assert response.status_code == 403


def test_get_settings_returns_current_effective_values(test_client) -> None:
    response = test_client.get(
        "/api/settings/company?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200
    payload = response.json()
    assert "phone" in payload
    assert "working_hours_schedule" in payload
    assert "widget" in payload
    assert "facts" in payload


def test_get_settings_404_for_unknown_company(test_client) -> None:
    response = test_client.get(
        "/api/settings/company?company_id=does_not_exist", headers=OPERATOR_HEADERS
    )
    assert response.status_code == 404


def test_post_settings_requires_operator_token(test_client) -> None:
    response = test_client.post("/api/settings/company?company_id=rosh_demo", json=_VALID_PAYLOAD)
    assert response.status_code == 403


def test_post_settings_rejects_bad_time_format(test_client) -> None:
    payload = dict(_VALID_PAYLOAD)
    payload["working_hours_schedule"] = dict(payload["working_hours_schedule"])
    payload["working_hours_schedule"]["mon"] = {"open": "9am", "close": "20:00"}
    response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422


def test_post_settings_rejects_empty_phone(test_client) -> None:
    payload = dict(_VALID_PAYLOAD)
    payload["phone"] = ""
    response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422


def test_post_settings_rejects_unknown_widget_position(test_client) -> None:
    payload = dict(_VALID_PAYLOAD)
    payload["widget"] = dict(payload["widget"])
    payload["widget"]["position"] = "top-center"
    response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422


def test_post_settings_saves_and_next_get_reflects_it_without_restart(test_client) -> None:
    """Ключевая проверка: после сохранения СЛЕДУЮЩИЙ же GET (тот же процесс, без рестарта)
    видит новые значения — то есть resolver.invalidate() реально сбрасывает кэш."""

    post_response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=_VALID_PAYLOAD, headers=OPERATOR_HEADERS
    )
    assert post_response.status_code == 200

    get_response = test_client.get(
        "/api/settings/company?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    payload = get_response.json()
    assert payload["phone"] == "+7 999 000-00-00"
    assert payload["address"] == "Новый адрес"
    assert payload["working_hours_schedule"]["sat"] is None
    assert payload["working_hours_schedule"]["mon"] == {"open": "09:00", "close": "20:00"}
    assert payload["widget"]["header_title"] == "Новый заголовок"
    assert payload["facts"]["oms"] is True


def test_post_settings_change_is_visible_in_a_real_chat_reply(test_client) -> None:
    """Не просто GET после POST — реальный разговор с ботом должен увидеть новый адрес без
    рестарта, ровно так, как его увидит настоящий клиент. Адрес, не телефон — keyword-путь
    для локации детерминирован и не зависит от классификации LLM (в тестах она mock)."""

    test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=_VALID_PAYLOAD, headers=OPERATOR_HEADERS
    )
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "какой у вас адрес"},
    )
    assert "Новый адрес" in response.json()["answer"]


def test_post_settings_rejects_doctor_without_name(test_client) -> None:
    payload = dict(_VALID_PAYLOAD)
    payload["doctors"] = [{"name": "", "specialty": "гинеколог", "schedule": "Пн 10:00-18:00"}]
    response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 422


def test_post_settings_saves_doctors_and_next_get_reflects_it(test_client) -> None:
    payload = dict(_VALID_PAYLOAD)
    payload["doctors"] = [
        {"name": "Доктор Тестовый", "specialty": "гинеколог", "schedule": "Пн 10:00-18:00"},
        {"name": "Доктор Второй", "specialty": "", "schedule": ""},
    ]
    post_response = test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )
    assert post_response.status_code == 200
    assert post_response.json()["doctors"] == payload["doctors"]

    get_response = test_client.get(
        "/api/settings/company?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    assert get_response.json()["doctors"] == payload["doctors"]


def test_post_settings_empty_doctors_list_clears_roster(test_client) -> None:
    payload_with_doctor = dict(_VALID_PAYLOAD)
    payload_with_doctor["doctors"] = [{"name": "Доктор Тестовый", "specialty": "", "schedule": ""}]
    test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload_with_doctor, headers=OPERATOR_HEADERS
    )

    payload_no_doctors = dict(_VALID_PAYLOAD)
    payload_no_doctors["doctors"] = []
    test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload_no_doctors, headers=OPERATOR_HEADERS
    )

    get_response = test_client.get(
        "/api/settings/company?company_id=rosh_demo", headers=OPERATOR_HEADERS
    )
    assert get_response.json()["doctors"] == []


def test_post_reset_block_requires_operator_token(test_client) -> None:
    response = test_client.post("/api/settings/company/reset-block?company_id=rosh_demo&block=hours")
    assert response.status_code == 403


def test_post_reset_block_404_for_unknown_company(test_client) -> None:
    response = test_client.post(
        "/api/settings/company/reset-block?company_id=does_not_exist&block=hours",
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 404


def test_post_reset_block_422_for_unknown_block_name(test_client) -> None:
    response = test_client.post(
        "/api/settings/company/reset-block?company_id=rosh_demo&block=not_a_real_block",
        headers=OPERATOR_HEADERS,
    )
    assert response.status_code == 422


def test_post_reset_block_restores_hours_to_state_before_last_save(test_client) -> None:
    """Сценарий пользователя: сохранили часы 09-20, потом 09-21, reset блока часов — назад к 20."""

    payload_v1 = dict(_VALID_PAYLOAD)
    payload_v1["working_hours_schedule"] = dict(_VALID_PAYLOAD["working_hours_schedule"])
    payload_v1["working_hours_schedule"]["mon"] = {"open": "09:00", "close": "20:00"}
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v1, headers=OPERATOR_HEADERS)

    payload_v2 = dict(_VALID_PAYLOAD)
    payload_v2["working_hours_schedule"] = dict(_VALID_PAYLOAD["working_hours_schedule"])
    payload_v2["working_hours_schedule"]["mon"] = {"open": "09:00", "close": "21:00"}
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v2, headers=OPERATOR_HEADERS)

    reset_response = test_client.post(
        "/api/settings/company/reset-block?company_id=rosh_demo&block=hours", headers=OPERATOR_HEADERS
    )
    assert reset_response.status_code == 200
    assert reset_response.json()["working_hours_schedule"]["mon"] == {"open": "09:00", "close": "20:00"}


def test_post_reset_block_leaves_other_blocks_untouched(test_client) -> None:
    """reset блока "hours" не должен откатывать телефон, даже если его сохранили тем же POST."""

    payload_v1 = dict(_VALID_PAYLOAD)
    payload_v1["phone"] = "+7 111"
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v1, headers=OPERATOR_HEADERS)

    payload_v2 = dict(_VALID_PAYLOAD)
    payload_v2["phone"] = "+7 222"
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v2, headers=OPERATOR_HEADERS)

    reset_response = test_client.post(
        "/api/settings/company/reset-block?company_id=rosh_demo&block=hours", headers=OPERATOR_HEADERS
    )
    assert reset_response.json()["phone"] == "+7 222"


def test_post_reset_block_new_get_reflects_reset_without_restart(test_client) -> None:
    payload_v1 = dict(_VALID_PAYLOAD)
    payload_v1["widget"] = dict(_VALID_PAYLOAD["widget"])
    payload_v1["widget"]["header_title"] = "Заголовок 1"
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v1, headers=OPERATOR_HEADERS)

    payload_v2 = dict(_VALID_PAYLOAD)
    payload_v2["widget"] = dict(_VALID_PAYLOAD["widget"])
    payload_v2["widget"]["header_title"] = "Заголовок 2"
    test_client.post("/api/settings/company?company_id=rosh_demo", json=payload_v2, headers=OPERATOR_HEADERS)

    test_client.post("/api/settings/company/reset-block?company_id=rosh_demo&block=widget", headers=OPERATOR_HEADERS)

    get_response = test_client.get("/api/settings/company?company_id=rosh_demo", headers=OPERATOR_HEADERS)
    assert get_response.json()["widget"]["header_title"] == "Заголовок 1"


def test_post_settings_new_doctor_is_visible_in_a_real_chat_reply(test_client) -> None:
    """Не просто GET после POST — реальный вопрос про врачей должен упомянуть добавленного
    через "Настройки" доктора, ровно так, как его увидит настоящий клиент."""

    payload = dict(_VALID_PAYLOAD)
    payload["doctors"] = [
        {"name": "Доктор Уникальное Имя", "specialty": "гинеколог", "schedule": "Пн 10:00-18:00"},
    ]
    test_client.post(
        "/api/settings/company?company_id=rosh_demo", json=payload, headers=OPERATOR_HEADERS
    )

    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "какие врачи у вас работают"},
    )
    assert "Доктор Уникальное Имя" in response.json()["answer"]
