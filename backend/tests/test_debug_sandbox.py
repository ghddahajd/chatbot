"""отладка = настоящий чат: тот же ответ, что увидит человек, цепочки сообщений, и ничего наружу —
ни заявок, ни Telegram, ни аналитики, ни новых сессий."""

from __future__ import annotations

import json

import pytest

from .test_ops_bot import _open_always

TRACE_URL = "/api/debug/trace?token=demo-operator-token"


def _trace(test_client, **body) -> dict:
    response = test_client.post(TRACE_URL, json={"company_id": "rosh_demo", **body})
    assert response.status_code == 200
    return response.json()


def _first_variant(monkeypatch) -> None:
    # вариант фразы выбирается по коду диалога, а у отладки и чата коды разные
    monkeypatch.setattr("app.knowledge._stable_choice_index", lambda seed, size: 0)
    monkeypatch.setattr("app.knowledge.random.choice", lambda items: items[0])


def _lines(path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


@pytest.mark.parametrize(
    "message",
    ["сколько стоит чистка лица", "у меня болит голова", "что такое биоревитализация", "привет"],
)
def test_trace_answers_exactly_like_the_chat(test_client, monkeypatch, message: str) -> None:
    _first_variant(monkeypatch)

    chat = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "message": message}).json()
    trace = _trace(test_client, message=message)

    assert (trace["final_answer"], trace["final_action"]) == (chat["answer"], chat["action"])


def test_trace_runs_a_whole_dialog_and_saves_nothing(test_client) -> None:
    _open_always(test_client)
    state = test_client.app.state
    leads_before = _lines(state.lead_service.leads_file)
    analytics_before = _lines(state.analytics_service.analytics_file)
    sessions_before = len(test_client.portal.call(state.session_store.list_all))

    trace = _trace(test_client, messages=["хочу записаться", "Завтра", "89001234567"])

    assert [turn["message"] for turn in trace["turns"]] == ["хочу записаться", "Завтра", "89001234567"]
    lead_effects = [effect for effect in trace["turns"][-1]["effects"] if effect["service"] == "lead"]
    assert lead_effects and lead_effects[0]["lead"]["phone_given"] is True
    assert trace["lead_preview"] is True
    assert "89001234567" not in json.dumps([turn["effects"] for turn in trace["turns"]], ensure_ascii=False)
    assert _lines(state.lead_service.leads_file) == leads_before
    assert _lines(state.analytics_service.analytics_file) == analytics_before
    assert len(test_client.portal.call(state.session_store.list_all)) == sessions_before


def test_trace_hands_over_as_if_telegram_works(test_client) -> None:
    # в песочнице Telegram «доставляет» — иначе передача пошла бы по ветке «администратор недоступен»
    _open_always(test_client)

    trace = _trace(test_client, message="Хочу поговорить с менеджером")

    assert trace["turns"][-1]["status"] == "WAITING_OPERATOR"
    telegram_calls = [effect["call"] for effect in trace["turns"][-1]["effects"] if effect["service"] == "telegram"]
    assert "post_operator_queue_card" in telegram_calls


def test_trace_needs_a_message(test_client) -> None:
    response = test_client.post(TRACE_URL, json={"company_id": "rosh_demo"})

    assert response.status_code == 400
