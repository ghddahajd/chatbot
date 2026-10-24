"""админка: дашборд разбирает файл событий один раз и не держит чат; «Чаты» — период и «Показать ещё»."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from app import analytics
from app.analytics import AnalyticsService
from app.utils.jsonl import append_jsonl

OPERATOR_HEADERS = {"x-operator-token": "demo-operator-token"}


def test_dashboard_reads_events_file_once(test_client, managed_env, monkeypatch) -> None:
    analytics_file = test_client.app.state.analytics_service.analytics_file
    append_jsonl(analytics_file, {"timestamp": datetime.utcnow().isoformat(), "company_id": "rosh_demo", "event_type": "chat_opened"})
    reads = []
    real_read = analytics.read_jsonl
    monkeypatch.setattr(analytics, "read_jsonl", lambda path: (reads.append(path), real_read(path))[1])

    response = test_client.get("/api/analytics/dashboard?company_id=rosh_demo&days=30", headers=OPERATOR_HEADERS)

    assert response.status_code == 200
    # на проде файл ~18 МБ: раньше его разбирал каждый из ~15 подсчётов
    assert reads.count(analytics_file) == 1


def test_chat_answers_while_dashboard_is_counting(test_client, monkeypatch) -> None:
    real_summary = AnalyticsService.summary

    def slow_summary(self, *args, **kwargs):
        time.sleep(1.5)
        return real_summary(self, *args, **kwargs)

    monkeypatch.setattr(AnalyticsService, "summary", slow_summary)
    dashboard = threading.Thread(
        target=lambda: test_client.get("/api/analytics/dashboard?company_id=rosh_demo", headers=OPERATOR_HEADERS)
    )
    dashboard.start()
    time.sleep(0.3)

    started = time.perf_counter()
    reply = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": None, "message": "привет"})
    waited = time.perf_counter() - started
    dashboard.join()

    assert reply.status_code == 200
    # подсчёт дашборда идёт в своём потоке — посетитель не ждёт, пока админка досчитает
    assert waited < 1.0


def _archived(archive_file, session_id: str, closed_at: datetime) -> None:
    append_jsonl(
        archive_file,
        {
            "session_id": session_id,
            "company_id": "rosh_import_demo",
            "status": "CLOSED",
            "lead_requested": False,
            "operator_requested": False,
            "closed_at": closed_at.isoformat(),
            "messages": [{"role": "user", "text": f"вопрос {session_id}", "created_at": closed_at.isoformat()}],
        },
    )


def test_chats_page_follows_period_and_shows_more(tmp_path) -> None:
    archive_file = tmp_path / "conversations_archive.jsonl"
    now = datetime.utcnow()
    for index in range(5):
        _archived(archive_file, f"recent-{index}", now - timedelta(days=1, minutes=index))
    _archived(archive_file, "old", now - timedelta(days=40))
    service = AnalyticsService(
        analytics_file=tmp_path / "analytics.jsonl",
        leads_file=tmp_path / "leads.jsonl",
        conversations_archive_file=archive_file,
    )

    first = service.conversations_page([], company_id="rosh_import_demo", days=30, limit=2)
    more = service.conversations_page([], company_id="rosh_import_demo", days=30, offset=2, limit=2)
    whole_time = service.conversations_page([], company_id="rosh_import_demo", days=3650, limit=50)
    custom = service.conversations_page(
        [], company_id="rosh_import_demo", start=now - timedelta(days=45), end=now - timedelta(days=30), limit=50
    )

    assert first["total"] == 5  # 40-дневный чат вне периода
    assert [item["session_id"] for item in first["conversations"]] == ["recent-0", "recent-1"]
    assert [item["session_id"] for item in more["conversations"]] == ["recent-2", "recent-3"]
    assert whole_time["total"] == 6
    assert [item["session_id"] for item in custom["conversations"]] == ["old"]


def test_chats_route_passes_period_and_offset(test_client) -> None:
    for message in ("привет", "сколько стоит чистка лица", "где вы находитесь"):
        test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": None, "message": message})

    first = test_client.get("/api/analytics/chats?company_id=rosh_demo&days=7&limit=2", headers=OPERATOR_HEADERS).json()
    rest = test_client.get("/api/analytics/chats?company_id=rosh_demo&days=7&limit=2&offset=2", headers=OPERATOR_HEADERS).json()

    assert first["total"] >= 3
    assert len(first["conversations"]) == 2
    shown = {item["session_id"] for item in first["conversations"]}
    assert not shown & {item["session_id"] for item in rest["conversations"]}
