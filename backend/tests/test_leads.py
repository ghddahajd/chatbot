"""проверки детерминированной классификации лида (reason/lead_trigger/recent_messages)."""

from app.leads import classify_lead_reason, lead_trigger_for, recent_messages_for
from app.models import Message, MessageRole, Session


def _session(*, last_intent: str | None = None, messages: list[str] | None = None) -> Session:
    session = Session(company_id="rosh_demo", last_intent=last_intent)
    for text in messages or []:
        session.messages.append(Message(role=MessageRole.USER, text=text))
    return session


def test_classify_lead_reason_booking_wins_over_intent() -> None:
    assert classify_lead_reason(last_intent="price_question", is_booking_request=True) == "booking"


def test_classify_lead_reason_medical_risk() -> None:
    assert classify_lead_reason(last_intent="regulated_advice", is_booking_request=False) == "medical_risk"


def test_classify_lead_reason_price_question() -> None:
    assert classify_lead_reason(last_intent="price_question", is_booking_request=False) == "price_question"


def test_classify_lead_reason_default_commercial_interest() -> None:
    assert classify_lead_reason(last_intent="ok", is_booking_request=False) == "commercial_interest"


def test_lead_trigger_booking_beats_operator_flow() -> None:
    assert lead_trigger_for(is_booking_request=True, is_operator_flow=True) == "booking_request"


def test_lead_trigger_operator_handoff() -> None:
    assert lead_trigger_for(is_booking_request=False, is_operator_flow=True) == "operator_handoff"


def test_lead_trigger_regulated_beats_operator_flow() -> None:
    assert (
        lead_trigger_for(
            is_booking_request=False,
            is_operator_flow=True,
            is_regulated_flow=True,
        )
        == "regulated_advice"
    )


def test_lead_trigger_default_ask_contact() -> None:
    assert lead_trigger_for(is_booking_request=False, is_operator_flow=False) == "ask_contact"


def test_recent_messages_for_limits_and_serializes() -> None:
    session = _session(messages=[f"msg-{i}" for i in range(12)])

    recent = recent_messages_for(session, limit=5)

    assert len(recent) == 5
    assert recent[-1] == {"role": "user", "text": "msg-11"}
    assert recent[0] == {"role": "user", "text": "msg-7"}
