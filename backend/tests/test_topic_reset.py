"""после ответа «все услуги» или «все цены» разговор уже не об одной услуге: «хочу уточнить цену»
не должно снова отвечать про услугу из начала переписки (ни из памяти сессии, ни из истории)."""

from __future__ import annotations

from app.models import Message, MessageRole, Session
from app.policy.extractors import last_service_from_history

from .test_booking_tiles import _chat


def test_price_question_after_the_services_list_forgets_the_old_service(test_client) -> None:
    first = _chat(test_client, "сколько стоит чистка лица")
    _chat(test_client, "покажи услуги", first["session_id"])

    payload = _chat(test_client, "хочу уточнить цену", first["session_id"])

    session = test_client.portal.call(test_client.app.state.session_store.get, first["session_id"])
    assert session.last_intent == "price_question_no_service"
    assert not payload["answer"].startswith("Чистка лица")


def test_naming_a_service_after_the_list_still_answers_about_it(test_client) -> None:
    first = _chat(test_client, "сколько стоит консультация")
    _chat(test_client, "покажи услуги", first["session_id"])

    payload = _chat(test_client, "сколько стоит чистка лица", first["session_id"])

    assert payload["answer"] == _chat(test_client, "сколько стоит чистка лица")["answer"]


def _messages(*texts: str) -> list[Message]:
    roles = [MessageRole.USER, MessageRole.ASSISTANT]
    return [Message(role=roles[index % 2], text=text) for index, text in enumerate(texts)]


def test_history_before_the_overview_is_not_searched(knowledge_base) -> None:
    session = Session(company_id="rosh_demo")
    session.messages = _messages(
        "сколько стоит чистка лица", "«Чистки» — от …",
        "Какие услуги у вас есть и сколько стоят?", "Популярные направления и цены: …",
        "хочу уточнить цену",
    )
    assert last_service_from_history(session, knowledge_base) is not None  # без отметки нашла бы чистку

    session.topic_reset_at = 4  # бот показал все цены — дальше ищем только после этого места

    assert last_service_from_history(session, knowledge_base) is None
