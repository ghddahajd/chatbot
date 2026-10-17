"""пока бот ждёт номер для записи или обратного звонка, кризис, угроза жизни и жалоба не тонут в сборе
номера: отвечают те же правила безопасности, что и вне записи."""

from __future__ import annotations

import pytest

from .test_booking_tiles import _chat


def _session(test_client, session_id: str):
    return test_client.portal.call(test_client.app.state.session_store.get, session_id)


@pytest.mark.parametrize(
    ("start", "message", "intent"),
    [
        ("хочу записаться", "не хочу жить", "self_harm_crisis"),
        ("хочу записаться", "думаю о суициде", "self_harm_crisis"),
        ("перезвоните мне", "не хочу жить", "self_harm_crisis"),
        ("хочу записаться", "после укола задыхаюсь", "regulated_advice"),
        ("хочу записаться", "хочу пожаловаться на врача", "complaint"),
    ],
)
def test_safety_wins_over_collecting_the_phone(test_client, start: str, message: str, intent: str) -> None:
    first = _chat(test_client, start)
    assert _session(test_client, first["session_id"]).pending_action  # бот ждёт номер

    payload = _chat(test_client, message, first["session_id"])

    assert _session(test_client, first["session_id"]).last_intent == intent
    assert payload["booking_form"] is None


def test_a_plain_no_still_cancels_the_booking(test_client) -> None:
    first = _chat(test_client, "хочу записаться")

    payload = _chat(test_client, "нет, не хочу", first["session_id"])

    assert "не передаю" in payload["answer"]
