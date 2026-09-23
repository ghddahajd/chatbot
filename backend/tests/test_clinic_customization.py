"""кусок 5 (2026-09-23): клиника сама меняет тексты бота, надписи виджета, подписи кнопок,
ссылку на политику и минуты ожидания администратора — во вкладке «Настройки», без выкатки."""

from __future__ import annotations

import json
import re

import pytest

from app.editable_texts import EDITABLE_TEXTS, RENAMABLE_BUTTONS, validate_text
from app.knowledge import DEFAULT_PHRASEBOOK

HEADERS = {"x-operator-token": "demo-operator-token"}
SETTINGS_URL = "/api/settings/company?company_id=rosh_demo"
ALWAYS_OPEN = {day: {"open": "00:00", "close": "23:59"} for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def _settings(test_client) -> dict:
    response = test_client.get(SETTINGS_URL, headers=HEADERS)
    assert response.status_code == 200
    return response.json()


def _save(test_client, **changes):
    """как делает вкладка: берём текущее, меняем нужное, отправляем целиком."""

    data = _settings(test_client)
    payload = {key: data[key] for key in ("phone", "address", "telegram_url", "website_url", "facts", "doctors")}
    payload["working_hours_schedule"] = ALWAYS_OPEN
    payload["widget"] = dict(data["widget"])
    payload.update(changes)
    return test_client.post(SETTINGS_URL, json=payload, headers=HEADERS)


def _chat(test_client, message: str, session_id: str | None = None) -> dict:
    return test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message}).json()


def _texts(data: dict) -> dict[str, dict]:
    return {item["key"]: item for group in data["texts"] for item in group["items"]}


# ---------------------------------------------------------------- что вообще отдаём клинике


def test_every_editable_text_exists_and_placeholders_match_the_default() -> None:
    for key, item in EDITABLE_TEXTS.items():
        assert key in DEFAULT_PHRASEBOOK, key
        default = DEFAULT_PHRASEBOOK[key]
        variants = default if isinstance(default, list) else [default]
        used = {name for text in variants for name in re.findall(r"\{([a-z_]+)\}", text)}
        assert used <= set(item.placeholders), key


@pytest.mark.parametrize(
    "key",
    ["self_harm_crisis", "bot_identity_confirm", "medical_referral", "regulated_soft_offer", "price_disclaimer", "fact_oms_yes", "objection_guarantee"],
)
def test_safety_medical_price_and_fact_texts_are_not_editable(key: str) -> None:
    assert key not in EDITABLE_TEXTS


@pytest.mark.parametrize(
    ("key", "text", "problem"),
    [
        ("greeting", "   ", "пустым"),
        ("greeting", "х" * 601, "600"),
        ("greeting", "Пишите на https://evil.test", "ссылок"),
        ("greeting", "Привет <b>всем</b>", "HTML"),
        ("greeting", "Звоните {phone}", "неизвестная подстановка {phone}"),
        ("clinic_contacts", "Телефон: {phne}", "неизвестная подстановка {phne}"),
        ("clinic_contacts", "Скобка { сама по себе", "фигурные скобки"),
    ],
)
def test_validation_explains_what_is_wrong(key: str, text: str, problem: str) -> None:
    assert problem in (validate_text(key, text) or "")


def test_validation_accepts_allowed_placeholders() -> None:
    assert validate_text("clinic_contacts", "{company_name}: звоните {phone}, часы — {working_hours}.") is None


# ---------------------------------------------------------------- вкладка «Настройки»


def test_settings_show_where_each_text_appears_and_its_default(test_client) -> None:
    data = _settings(test_client)
    greeting = _texts(data)["greeting"]

    assert [group["title"] for group in data["texts"]][0] == "Приветствие и о клинике"
    assert greeting["where"].startswith("Первое сообщение")
    assert greeting["default"] and greeting["value"] == greeting["default"]
    assert greeting["customized"] is False
    assert [button["original"] for button in data["button_labels"]] == list(RENAMABLE_BUTTONS)
    assert data["operator_wait_offer_minutes"] == 5


def test_changed_text_reaches_the_bot_without_a_deploy(test_client) -> None:
    assert _save(test_client, texts={"booking_when_prompt": ["Когда вам будет удобно прийти?"]}).status_code == 200

    assert _chat(test_client, "хочу записаться")["answer"] == "Когда вам будет удобно прийти?"
    assert _texts(_settings(test_client))["booking_when_prompt"]["customized"] is True


def test_greeting_variants_reach_the_widget(test_client) -> None:
    _save(test_client, texts={"greeting": ["Добрый день!", "Здравствуйте!"]})

    greeting = test_client.get("/api/widget/bootstrap?company_id=rosh_demo", headers={"origin": "http://localhost:5500"}).json()["greeting"]

    assert greeting in {"Добрый день!", "Здравствуйте!"}


def test_only_real_changes_are_stored_and_default_brings_it_back(test_client, managed_env) -> None:
    data = _settings(test_client)
    everything = {key: item["value"] for key, item in _texts(data).items()}  # вкладка шлёт все тексты
    everything["clarify"] = ["Что именно подсказать?"]
    _save(test_client, texts=everything)

    stored = json.loads((managed_env["temp_dir"] / "overrides" / "rosh_demo.json").read_text(encoding="utf-8"))
    assert stored["texts"] == {"clarify": "Что именно подсказать?"}

    everything["clarify"] = _texts(data)["clarify"]["default"]  # «Вернуть по умолчанию»
    _save(test_client, texts=everything)
    stored = json.loads((managed_env["temp_dir"] / "overrides" / "rosh_demo.json").read_text(encoding="utf-8"))
    assert stored["texts"] == {}


@pytest.mark.parametrize(
    ("texts", "detail"),
    [
        ({"self_harm_crisis": ["Всё будет хорошо"]}, "менять нельзя"),
        ({"clinic_contacts": ["Звоните {phne}"]}, "«Телефон и контакты»: неизвестная подстановка"),
    ],
)
def test_forbidden_or_broken_texts_are_rejected(texts: dict, detail: str, test_client) -> None:
    response = _save(test_client, texts=texts)

    assert response.status_code == 422
    assert detail in response.json()["detail"]


def test_old_settings_tab_keeps_saved_texts_buttons_and_minutes(test_client, managed_env) -> None:
    _save(
        test_client,
        texts={"clarify": ["Что именно подсказать?"]},
        button_labels={"Посмотреть услуги": "Все услуги"},
        operator_wait_offer_minutes=3,
        privacy_policy_url="https://clinic.test/policy",
    )

    _save(test_client)  # старая версия вкладки: новых полей не присылает

    data = _settings(test_client)
    assert _texts(data)["clarify"]["value"] == ["Что именно подсказать?"]
    assert {b["original"]: b["label"] for b in data["button_labels"]}["Посмотреть услуги"] == "Все услуги"
    assert data["operator_wait_offer_minutes"] == 3
    assert data["privacy_policy_url"] == "https://clinic.test/policy"


def test_reset_block_undoes_the_last_texts_save(test_client) -> None:
    _save(test_client, texts={"clarify": ["Первый вариант"]})
    _save(test_client, texts={"clarify": ["Второй вариант"]})

    test_client.post(SETTINGS_URL.replace("/company?", "/company/reset-block?") + "&block=texts", headers=HEADERS)

    assert _texts(_settings(test_client))["clarify"]["value"] == ["Первый вариант"]


# ---------------------------------------------------------------- кнопки, надписи, политика, минуты


def test_button_rename_changes_only_the_label(test_client) -> None:
    _save(test_client, button_labels={"Посмотреть услуги": "Все услуги клиники"})

    payload = _chat(test_client, "сколько стоит липосакция")
    actions = {action["label"]: action["value"] for action in payload["quick_actions"]}

    assert "Все услуги клиники" in actions
    assert "Посмотреть услуги" not in actions
    assert actions["Все услуги клиники"] == "Покажи список услуг"  # бот понимает нажатие как раньше


def test_only_known_buttons_can_be_renamed(test_client) -> None:
    response = _save(test_client, button_labels={"Сегодня": "Прямо сейчас"})

    assert response.status_code == 422


def test_widget_labels_and_policy_link_reach_the_widget(test_client) -> None:
    widget = dict(_settings(test_client)["widget"])
    widget.update({"launcher_label": "Спросить", "status_online": "онлайн", "input_placeholder": "Ваш вопрос…", "operator_label": "Администратор"})
    assert _save(test_client, widget=widget, privacy_policy_url="https://clinic.test/policy").status_code == 200

    data = test_client.get("/api/widget/bootstrap?company_id=rosh_demo", headers={"origin": "http://localhost:5500"}).json()

    assert {key: data["widget_config"][key] for key in ("launcher_label", "status_online", "input_placeholder", "operator_label")} == {
        "launcher_label": "Спросить", "status_online": "онлайн", "input_placeholder": "Ваш вопрос…", "operator_label": "Администратор",
    }
    assert data["privacy_policy_url"] == "https://clinic.test/policy"


def test_policy_link_must_be_https(test_client) -> None:
    assert _save(test_client, privacy_policy_url="http://clinic.test/policy").status_code == 422


@pytest.mark.parametrize("minutes", [0, 61])
def test_wait_minutes_are_bounded(minutes: int, test_client) -> None:
    assert _save(test_client, operator_wait_offer_minutes=minutes).status_code == 422


def test_wait_minutes_change_when_the_offer_appears(test_client) -> None:
    from datetime import datetime, timedelta

    _save(test_client, operator_wait_offer_minutes=2)
    session_id = _chat(test_client, "оператор")["session_id"]
    _chat(test_client, "Да, оператора", session_id)
    stored = test_client.app.state.session_store._sessions[session_id]
    stored.messages[-1].created_at = datetime.utcnow() - timedelta(minutes=3)  # при 5 минутах предложения ещё не было бы

    followup = _chat(test_client, "а вы ещё тут?", session_id)

    assert "Да, продолжить с ботом" in {action["value"] for action in followup["quick_actions"]}


def test_widget_source_reads_the_new_labels() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "widget" / "widget.js").read_text(encoding="utf-8")

    assert "c.launcher_label" in source and "cfg.status_online" in source and "cfg.input_placeholder" in source
    assert 'label.textContent = cfg.operator_label' in source
