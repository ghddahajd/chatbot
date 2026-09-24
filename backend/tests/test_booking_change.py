"""«отменить / перенести запись» — про уже существующую запись: её ведёт администратор по номеру,
на который человек записан. Раньше бот принимал такую просьбу за новую запись («Когда вам удобно?»)."""

from __future__ import annotations

import json

import pytest

from app.knowledge import normalize_text
from app.models import DaySchedule
from app.policy import is_booking_cancel_only, is_booking_change_request

HEADERS = {"x-operator-token": "demo-operator-token"}
ALWAYS_OPEN = {day: {"open": "00:00", "close": "23:59"} for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def _chat(test_client, message: str, session_id: str | None = None) -> dict:
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message})
    assert response.status_code == 200
    return response.json()


def _company(test_client):
    return test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company


def _last_lead(managed_env) -> dict:
    return json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])


class _CardBridge:
    enabled = True

    def __init__(self) -> None:
        self.client_cards: list[str] = []

    async def forward_client_message(self, session_id: str, text: str) -> None:
        return None

    async def post_operator_queue_card(self, **_kwargs) -> None:
        return None

    async def post_client_lead_card(self, card_text, *, session_id: str = ""):
        self.client_cards.append(card_text)


# ---------------------------------------------------------------- что считаем отменой / переносом


@pytest.mark.parametrize(
    "message",
    [
        "отмените запись",
        "хочу отменить запись",
        "хочу перенести запись",
        "можно перенести приём на другой день",
        "не смогу прийти, перенесите",
        "перенесите меня на пятницу",
        "я записана на завтра, но не смогу прийти",
        "как отменить запись?",
        "отменить визит",
        "можно перезаписаться на другое время",
        "нужно сдвинуть запись на час",
        "хочу поменять время записи",
    ],
)
def test_cancel_or_reschedule_is_recognised(message: str) -> None:
    assert is_booking_change_request(normalize_text(message))


@pytest.mark.parametrize(
    "message",
    [
        "хочу записаться",
        "запишите на пятницу",
        "можно записаться на другой день?",
        "отмена",
        "перенос",
        "как переносится процедура?",  # про самочувствие, не про запись
        "я перенесла операцию, можно на пилинг?",
        "не смогу прийти лично, есть онлайн-консультация?",
        "хочу записаться на пилинг, а если что можно будет перенести?",
        "а если я опоздаю?",
    ],
)
def test_new_booking_and_lookalikes_are_not_a_change(message: str) -> None:
    assert not is_booking_change_request(normalize_text(message))


def test_only_plain_cancel_counts_as_dropping_the_booking_in_progress() -> None:
    assert is_booking_cancel_only(normalize_text("отмените запись"))
    assert not is_booking_cancel_only(normalize_text("хочу перенести запись"))


# ---------------------------------------------------------------- ответ и заявка


def test_change_request_asks_for_the_booked_phone_and_offers_to_call(test_client) -> None:
    _company(test_client).working_hours_schedule = {}  # всегда открыто
    payload = _chat(test_client, "хочу перенести запись")

    assert payload["action"] == "ask_contact"
    assert "номер телефона, на который вы записаны" in payload["answer"]
    assert payload["answer"].endswith(f"Или позвоните нам: {_company(test_client).phone}.")
    assert "Когда вам удобно" not in payload["answer"]
    assert payload["quick_actions"] == []


def test_phone_after_the_request_sends_a_separate_card(test_client, managed_env) -> None:
    bridge = _CardBridge()
    test_client.app.state.telegram_bridge_service = bridge
    first = _chat(test_client, "сколько стоит чистка лица")  # услуга из прошлого вопроса не должна попасть в карточку
    _chat(test_client, "хочу отменить запись", first["session_id"])

    done = _chat(test_client, "Анна 89001234567", first["session_id"])

    assert done["lead_created"] is True
    assert done["answer"] == "Спасибо. Передали менеджеру — он найдёт вашу запись и свяжется с вами."
    card = bridge.client_cards[-1]
    assert card.startswith("🔁 Перенос или отмена записи")
    assert "Новая запись" not in card and "Услуга:" not in card
    lead = _last_lead(managed_env)
    assert (lead["reason"], lead["phone"]) == ("booking_change", "+79001234567")


def test_phone_in_the_same_message(test_client, managed_env) -> None:
    bridge = _CardBridge()
    test_client.app.state.telegram_bridge_service = bridge

    payload = _chat(test_client, "перенесите запись, мой номер 89001234567")

    assert payload["lead_created"] is True
    assert bridge.client_cards[-1].startswith("🔁 Перенос или отмена записи")
    assert _last_lead(managed_env)["reason"] == "booking_change"


def test_at_night_the_phone_comes_with_working_hours(test_client) -> None:
    """ночью по номеру никто не ответит — подсказываем, когда звонить."""

    company = _company(test_client)
    closed = DaySchedule(open="00:00", close="00:01")
    company.working_hours_schedule = {day: closed for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

    payload = _chat(test_client, "хочу перенести запись")

    assert payload["action"] == "ask_contact"
    assert "номер телефона, на который вы записаны" in payload["answer"]
    assert payload["answer"].endswith(f"Или позвоните нам в часы работы ({company.working_hours}): {company.phone}.")


def test_asking_for_a_contact_does_not_swallow_a_cancel_request(test_client, managed_env) -> None:
    """«отменить» — ещё и слово отказа; в ожидании контакта это было бы «Хорошо, контакт пока не берём»."""

    first = _chat(test_client, "оставить телефон")

    payload = _chat(test_client, "хочу отменить запись", first["session_id"])

    assert "номер телефона, на который вы записаны" in payload["answer"]
    assert _chat(test_client, "89001234567", first["session_id"])["lead_created"] is True
    assert _last_lead(managed_env)["reason"] == "booking_change"


# ---------------------------------------------------------------- во время оформления новой записи


def test_cancel_during_a_new_booking_drops_that_booking(test_client) -> None:
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, "отмените запись", first["session_id"])

    assert payload["answer"].startswith("Хорошо, заявку пока не передаю")


def test_reschedule_during_a_new_booking_is_about_the_existing_one(test_client, managed_env) -> None:
    bridge = _CardBridge()
    test_client.app.state.telegram_bridge_service = bridge
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Завтра", first["session_id"])

    payload = _chat(test_client, "ой, мне вообще-то перенести запись надо", first["session_id"])
    assert "номер телефона, на который вы записаны" in payload["answer"]
    _chat(test_client, "89001234567", first["session_id"])

    assert "Когда удобно" not in bridge.client_cards[-1]  # «Завтра» было про новую запись, не про перенос
    assert _last_lead(managed_env)["reason"] == "booking_change"


def test_booking_still_starts_with_when(test_client) -> None:
    assert _chat(test_client, "хочу записаться")["answer"] == "Когда вам удобно?"


# ---------------------------------------------------------------- клиника меняет текст сама


def test_clinic_can_change_the_text_and_the_phone_is_still_added(test_client) -> None:
    settings_url = "/api/settings/company?company_id=rosh_demo"
    data = test_client.get(settings_url, headers=HEADERS).json()
    payload = {key: data[key] for key in ("phone", "address", "telegram_url", "website_url", "facts", "doctors")}
    payload.update(working_hours_schedule=ALWAYS_OPEN, widget=dict(data["widget"]))
    payload["texts"] = {"booking_change_prompt": ["Перенесём или отменим. Напишите номер, на который записывались."]}
    assert test_client.post(settings_url, json=payload, headers=HEADERS).status_code == 200

    answer = _chat(test_client, "отмените запись")["answer"]

    assert answer.startswith("Перенесём или отменим. Напишите номер, на который записывались.")
    assert "Или позвоните нам" in answer
