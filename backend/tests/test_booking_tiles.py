"""созвон с РОШ 2026-09-23: запись в два шага. «Когда вам удобно?» + плитки Сегодня / Завтра /
На этой неделе / Другое → текст под выбор → только телефон → «заявка принята». Если время уже
названо в самой просьбе («на завтра», «на вторник»), плитки не спрашиваем."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import DaySchedule

TILES = ["Сегодня", "Завтра", "На этой неделе", "Другое"]
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _chat(test_client, message: str, session_id: str | None = None) -> dict:
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message})
    assert response.status_code == 200
    return response.json()


def _labels(payload: dict) -> list[str]:
    return [item["label"] for item in payload["quick_actions"]]


def _set_open(test_client, is_open: bool) -> None:
    company = test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company
    closed_all_day = DaySchedule(open="00:00", close="00:01")
    company.working_hours_schedule = {} if is_open else {day: closed_all_day for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def _last_lead(managed_env) -> dict:
    return json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])


def test_booking_starts_with_when_and_four_tiles(test_client) -> None:
    payload = _chat(test_client, "хочу записаться")

    assert payload["answer"] == "Когда вам удобно?"
    assert _labels(payload) == TILES  # без «Оставить телефон» и «Позвать менеджера»


def test_welcome_card_booking_on_real_rosh_data(test_client, managed_env) -> None:
    """карточка «Записаться на приём» шлёт «Хочу записаться на консультацию» — у РОШ это услуга."""

    import shutil

    source = BACKEND_DIR / "data" / "clients" / "rosh_import_demo"
    if not (source / "config.yaml").exists():
        pytest.skip("нет локальной копии данных РОШ")
    shutil.copytree(source, managed_env["clients_dir"] / "rosh_import_demo")
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_import_demo", "session_id": None, "message": "Хочу записаться на консультацию"},
    )

    assert response.json()["answer"] == "Когда вам удобно?"
    assert _labels(response.json()) == TILES


@pytest.mark.parametrize(
    ("tile", "expected_start"),
    [
        ("Завтра", "Хорошо, подберём время на завтра."),
        ("На этой неделе", "Хорошо, подберём удобное время в ближайшие дни."),
        ("Другое", "Хорошо, подберём удобное время в ближайшие дни."),
    ],
)
def test_each_tile_gets_its_text_and_asks_only_phone(tile: str, expected_start: str, test_client) -> None:
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, tile, first["session_id"])

    assert payload["answer"].startswith(expected_start)
    assert "номер телефона" in payload["answer"]
    assert "имя" not in payload["answer"].lower()
    assert payload["quick_actions"] == []


@pytest.mark.parametrize(("is_open", "expected_start"), [(True, "Проверим возможность записи сегодня."), (False, "Сейчас мы не работаем.")])
def test_today_depends_on_working_hours(is_open: bool, expected_start: str, test_client) -> None:
    _set_open(test_client, is_open)
    first = _chat(test_client, "хочу записаться")

    assert _chat(test_client, "Сегодня", first["session_id"])["answer"].startswith(expected_start)


def test_phone_only_creates_lead_with_chosen_time_and_success_text(test_client, managed_env) -> None:
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Завтра", first["session_id"])

    payload = _chat(test_client, "+7 999 123-45-67", first["session_id"])

    assert payload["lead_created"] is True
    lead = _last_lead(managed_env)
    assert lead.get("name") in (None, "", "Не указано")
    assert "завтра" in lead["summary"].lower()


@pytest.mark.parametrize(("message", "preferred"), [("хочу записаться на завтра", "завтра"), ("запишите меня на чистку лица в пятницу утром", "пятницу утром")])
def test_time_named_in_request_skips_tiles(message: str, preferred: str, test_client, managed_env) -> None:
    first = _chat(test_client, message)

    assert "номер телефона" in first["answer"]
    assert first["quick_actions"] == []
    assert _chat(test_client, "89991234567", first["session_id"])["lead_created"] is True
    assert preferred in _last_lead(managed_env)["summary"].lower()


def test_typed_manager_request_still_works_without_the_button(test_client) -> None:
    _set_open(test_client, True)
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, "позовите менеджера", first["session_id"])

    assert payload["action"] in {"transfer_operator", "clarify"}
    assert "номер телефона" not in payload["answer"]


def test_typed_morning_evening_is_still_understood(test_client) -> None:
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, "утром", first["session_id"])

    assert payload["answer"].startswith("Хорошо, утром.")
    assert "номер телефона" in payload["answer"]


def test_booking_bridge_is_not_glued_to_the_when_question(test_client) -> None:
    payload = _chat(test_client, "хочу записаться")

    assert payload["answer"] == "Когда вам удобно?"
    assert "Оставить телефон" not in _labels(payload)


def test_rosh_booking_texts_in_real_data() -> None:
    import yaml

    config_path = BACKEND_DIR / "data" / "clients" / "rosh_import_demo" / "config.yaml"
    if not config_path.exists():
        pytest.skip("нет локальной копии данных РОШ")
    phrasebook = yaml.safe_load(config_path.read_text(encoding="utf-8"))["phrasebook"]

    assert phrasebook["booking_when_prompt"] == "Когда вам удобно?"
    for key in ("booking_when_today", "booking_when_today_closed", "booking_when_tomorrow", "booking_when_later"):
        assert "номер телефона" in phrasebook[key] and "имя" not in phrasebook[key].lower()
    assert phrasebook["booking_success_no_consultation"].startswith("Спасибо, ваша заявка принята!")
    assert "консультация врача" in phrasebook["booking_success"][0]  # напоминание — только для процедур


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


def test_lead_card_shows_request_service_and_time_not_the_tile(test_client, managed_env) -> None:
    """живой тест 2026-09-23: карточка была «Имя: Не указано … Заявка на запись: Сегодня» —
    без услуги, а сводкой шла нажатая плитка."""

    import shutil

    source = BACKEND_DIR / "data" / "clients" / "rosh_import_demo"
    if not (source / "config.yaml").exists():
        pytest.skip("нет локальной копии данных РОШ")
    shutil.copytree(source, managed_env["clients_dir"] / "rosh_import_demo")
    bridge = _CardBridge()
    test_client.app.state.telegram_bridge_service = bridge

    def chat(message: str, session_id: str | None = None) -> dict:
        body = {"company_id": "rosh_import_demo", "session_id": session_id, "message": message}
        return test_client.post("/api/chat/message", json=body).json()

    company = test_client.app.state.knowledge_base_resolver.get("rosh_import_demo", fallback=False).company
    company.working_hours_schedule = {}  # всегда открыто — ночной вариант проверяется ниже
    first = chat("Хочу записаться на консультацию")
    chat("Сегодня", first["session_id"])
    assert chat("+7 900 000-00-01", first["session_id"])["lead_created"] is True

    card = bridge.client_cards[-1]
    assert "Имя:" not in card
    assert "Услуга: Консультации" in card
    assert "Хочу записаться на консультацию" in card
    assert "Когда удобно: сегодня" in card
    assert "Заявка на запись: Сегодня" not in card


def test_today_chosen_after_hours_is_marked_for_the_admin(test_client, managed_env) -> None:
    _set_open(test_client, False)
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Сегодня", first["session_id"])

    assert _chat(test_client, "89001112233", first["session_id"])["lead_created"] is True
    assert "сегодня — запрос пришёл в нерабочее время" in _last_lead(managed_env)["summary"]


def test_lead_card_shows_the_name_when_the_person_gave_it(test_client) -> None:
    bridge = _CardBridge()
    test_client.app.state.telegram_bridge_service = bridge
    first = _chat(test_client, "хочу записаться")
    _chat(test_client, "Завтра", first["session_id"])

    assert _chat(test_client, "Иван +7 900 000-00-03", first["session_id"])["lead_created"] is True
    assert "Имя: Иван" in bridge.client_cards[-1]
