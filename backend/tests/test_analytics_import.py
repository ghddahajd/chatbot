import io
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.utils.jsonl import read_jsonl
from app.storage.analytics_import import snapshot_rows
from app.storage.analytics_sync import read_snapshot
from app.analytics import analytics_file_version, archive_old_analytics_events
from app.utils.jsonl import append_jsonl


def test_event_keys_survive_append_and_rewrite():
    old = {"company_id": "clinic", "timestamp": "2026-01-01T00:00:00", "event_type": "widget_impression"}
    repeated = {**old, "timestamp": "2026-02-01T00:00:00"}
    new = {**repeated, "event_id": str(uuid4())}

    def keys(*events, sort_keys=False):
        source = io.StringIO("".join(json.dumps(row, sort_keys=sort_keys) + "\n" for row in events))
        return [row["event_key"] for row in snapshot_rows(source, company="clinic")]

    first = keys(old, repeated, repeated)
    appended = keys(old, repeated, repeated, new)
    assert len(set(first)) == 3
    assert appended[:3] == first
    assert appended[3] == f"id:{new['event_id']}"
    assert keys(repeated, repeated, new, sort_keys=True) == appended[1:]


def test_snapshot_reports_whether_it_covers_all_companies(tmp_path):
    path = tmp_path / "analytics.jsonl"
    row = {"company_id": "clinic", "timestamp": "2026-01-01T00:00:00", "event_type": "chat_opened"}
    append_jsonl(path, row)
    version, events, complete = read_snapshot(path)
    assert version is not None and len(events) == 1 and complete

    append_jsonl(path, {**row, "company_id": "legacy"})
    version, events, complete = read_snapshot(path)
    assert version is not None and complete
    assert [event["company_id"] for event in events] == ["clinic", "legacy"]

    append_jsonl(path, {**row, "company_id": None})
    version, events, complete = read_snapshot(path)
    assert version is not None and len(events) == 2 and not complete


def test_chat_writes_analytics_without_analytics_database(test_client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "postgresql+asyncpg://127.0.0.1:1/unavailable")
    analytics_file = test_client.app.state.analytics_service.analytics_file
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "message": "привет"})
    assert response.status_code == 200
    events = [event for event in read_jsonl(analytics_file) if event["event_type"] == "message_answered"]
    assert len(events) == 1
    assert UUID(events[0]["event_id"]).version == 4


def test_prune_waits_for_new_file_version_and_preserves_unknown_clinic(tmp_path):
    events = tmp_path / "analytics.jsonl"
    rollup = tmp_path / "rollup.jsonl"
    timestamp = (datetime.now(timezone.utc) - timedelta(days=65)).replace(tzinfo=None).isoformat()
    old = {"company_id": "clinic", "timestamp": timestamp, "event_type": "widget_impression"}
    unknown = {**old, "company_id": "unknown"}
    append_jsonl(events, old)
    before_append = analytics_file_version(events)
    append_jsonl(events, unknown)
    assert archive_old_analytics_events(
        events, rollup, 60, synced_version=before_append, synced_companies={"clinic"},
    ) == 0
    assert read_jsonl(events) == [old, unknown]
    assert not rollup.exists()

    assert archive_old_analytics_events(
        events, rollup, 60, synced_version=analytics_file_version(events), synced_companies={"clinic"},
    ) == 1
    assert read_jsonl(events) == [unknown]
    assert len(read_jsonl(rollup)) == 1
