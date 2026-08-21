"""проверки чистки message_answered в analytics.jsonl (archive_old_analytics_events)."""

from datetime import datetime, timedelta

from app.analytics import archive_old_analytics_events
from app.utils.jsonl import append_jsonl, read_jsonl


def _event(
    *,
    event_type: str,
    timestamp: datetime,
    company_id: str = "rosh_import_demo",
    action: str | None = "answer",
    message: str = "some message text",
) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "company_id": company_id,
        "session_id": "s-1",
        "event_type": event_type,
        "message": message,
        "metadata": {"action": action} if action is not None else {},
    }


def test_archive_prunes_only_message_answered_past_retention(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    now = datetime.utcnow()
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now - timedelta(days=90)))
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=now - timedelta(days=5)))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 1
    remaining = read_jsonl(analytics_file)
    assert len(remaining) == 1
    assert remaining[0]["event_type"] == "message_answered"
    assert datetime.fromisoformat(remaining[0]["timestamp"]) > now - timedelta(days=10)


def test_archive_never_touches_other_event_types_regardless_of_age(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    now = datetime.utcnow()
    for event_type in ("unknown_question", "regulated_handoff", "operator_requested"):
        append_jsonl(
            analytics_file,
            _event(event_type=event_type, timestamp=now - timedelta(days=400), action=None),
        )

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    remaining = read_jsonl(analytics_file)
    assert {item["event_type"] for item in remaining} == {
        "unknown_question",
        "regulated_handoff",
        "operator_requested",
    }
    assert not rollup_file.exists()


def test_archive_rolls_up_counts_by_date_company_and_action_without_message_text(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    stale_day = datetime(2026, 1, 5, 10, 0, 0)
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="answer"),
    )
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="answer"),
    )
    append_jsonl(
        analytics_file,
        _event(event_type="message_answered", timestamp=stale_day, company_id="rosh_import_demo", action="clarify"),
    )

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 3
    rollup_entries = read_jsonl(rollup_file)
    by_action = {entry["action"]: entry["count"] for entry in rollup_entries}
    assert by_action == {"answer": 2, "clarify": 1}
    for entry in rollup_entries:
        assert "message" not in entry
        assert entry["date"] == "2026-01-05"
        assert entry["company_id"] == "rosh_import_demo"


def test_archive_noop_when_nothing_is_stale(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    append_jsonl(analytics_file, _event(event_type="message_answered", timestamp=datetime.utcnow()))

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert not rollup_file.exists()
    assert len(read_jsonl(analytics_file)) == 1


def test_archive_missing_file_is_noop(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert not rollup_file.exists()


def test_archive_keeps_message_answered_with_unparseable_timestamp(tmp_path) -> None:
    analytics_file = tmp_path / "analytics.jsonl"
    rollup_file = tmp_path / "analytics_daily_rollup.jsonl"
    append_jsonl(analytics_file, {"event_type": "message_answered", "timestamp": "not-a-date", "message": "x"})

    removed = archive_old_analytics_events(analytics_file, rollup_file, retention_days=60)

    assert removed == 0
    assert len(read_jsonl(analytics_file)) == 1
