import io
import json
from uuid import UUID, uuid4

from app.utils.jsonl import read_jsonl
from backend.scripts.check_analytics_import import snapshot_rows


def test_event_keys_survive_append_and_rewrite():
    tenant = uuid4()
    old = {"company_id": "clinic", "timestamp": "2026-01-01T00:00:00", "event_type": "widget_impression"}
    repeated = {**old, "timestamp": "2026-02-01T00:00:00"}
    new = {**repeated, "event_id": str(uuid4())}

    def keys(*events, sort_keys=False):
        source = io.StringIO("".join(json.dumps(row, sort_keys=sort_keys) + "\n" for row in events))
        return [row["event_key"] for row in snapshot_rows(source, tenant, "clinic")]

    first = keys(old, repeated, repeated)
    appended = keys(old, repeated, repeated, new)
    assert len(set(first)) == 3
    assert appended[:3] == first
    assert appended[3] == f"id:{new['event_id']}"
    assert keys(repeated, repeated, new, sort_keys=True) == appended[1:]


def test_chat_writes_analytics_without_analytics_database(test_client, monkeypatch):
    monkeypatch.setenv("ANALYTICS_DATABASE_URL", "postgresql+asyncpg://127.0.0.1:1/unavailable")
    analytics_file = test_client.app.state.analytics_service.analytics_file
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "message": "привет"})
    assert response.status_code == 200
    events = [event for event in read_jsonl(analytics_file) if event["event_type"] == "message_answered"]
    assert len(events) == 1
    assert UUID(events[0]["event_id"]).version == 4
