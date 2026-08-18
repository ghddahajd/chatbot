"""проверки детерминированной классификации лида (reason/lead_trigger/recent_messages)."""

from datetime import datetime, timedelta

from app.leads import archive_old_leads, classify_lead_reason, lead_trigger_for, recent_messages_for
from app.models import Message, MessageRole, Session
from app.utils.jsonl import append_jsonl, read_jsonl


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


def _lead_payload(*, name: str, timestamp: datetime) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": "rosh_demo",
        "session_id": "s-" + name,
        "name": name,
        "phone": "+70000000000",
        "summary": "test lead",
    }


def test_archive_old_leads_moves_only_entries_past_retention(tmp_path) -> None:
    leads_file = tmp_path / "leads.jsonl"
    archive_file = tmp_path / "leads_archive.jsonl"
    now = datetime.utcnow()
    append_jsonl(leads_file, _lead_payload(name="stale", timestamp=now - timedelta(days=120)))
    append_jsonl(leads_file, _lead_payload(name="fresh", timestamp=now - timedelta(days=5)))

    moved = archive_old_leads(leads_file, archive_file, retention_days=90)

    assert moved == 1
    assert [item["name"] for item in read_jsonl(leads_file)] == ["fresh"]
    assert [item["name"] for item in read_jsonl(archive_file)] == ["stale"]


def test_archive_old_leads_noop_when_nothing_is_stale(tmp_path) -> None:
    leads_file = tmp_path / "leads.jsonl"
    archive_file = tmp_path / "leads_archive.jsonl"
    append_jsonl(leads_file, _lead_payload(name="fresh", timestamp=datetime.utcnow()))

    moved = archive_old_leads(leads_file, archive_file, retention_days=90)

    assert moved == 0
    assert not archive_file.exists()
    assert [item["name"] for item in read_jsonl(leads_file)] == ["fresh"]


def test_archive_old_leads_missing_file_is_noop(tmp_path) -> None:
    leads_file = tmp_path / "leads.jsonl"
    archive_file = tmp_path / "leads_archive.jsonl"

    moved = archive_old_leads(leads_file, archive_file, retention_days=90)

    assert moved == 0
    assert not archive_file.exists()


def test_archive_old_leads_keeps_entries_with_unparseable_timestamp(tmp_path) -> None:
    leads_file = tmp_path / "leads.jsonl"
    archive_file = tmp_path / "leads_archive.jsonl"
    append_jsonl(leads_file, {"timestamp": "not-a-date", "name": "broken"})

    moved = archive_old_leads(leads_file, archive_file, retention_days=90)

    assert moved == 0
    assert [item["name"] for item in read_jsonl(leads_file)] == ["broken"]
