"""созвон с РОШ 2026-09-23: «из 22к переходов 113 открытий и 15 диалогов» — воронка считала
события, а не людей (каждая загрузка страницы = «виджет загружен»), и сигналы шли на адрес,
который режут блокировщики. Теперь: метка посетителя, путь /api/widget/event, страница открытия."""

from __future__ import annotations

import json

import pytest

from app.routes.widget import normalize_page

EVENT_URL = "/api/widget/event"
DASHBOARD_URL = "/api/analytics/dashboard?company_id=rosh_demo"
OPERATOR_HEADERS = {"x-operator-token": "demo-operator-token"}
VISITOR_A = "11111111-aaaa-4aaa-8aaa-111111111111"
VISITOR_B = "22222222-bbbb-4bbb-8bbb-222222222222"


def _event(test_client, kind: str, visitor: str = VISITOR_A, page: str = "/", **extra):
    body = {"company_id": "rosh_demo", "kind": kind, "visitor_id": visitor, "page": page, **extra}
    return test_client.post(EVENT_URL, json=body)


def _funnel(test_client) -> dict:
    response = test_client.get(DASHBOARD_URL, headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    return response.json()["funnel"]


def _stages(funnel: dict) -> dict:
    return {stage["label"]: stage for stage in funnel["stages"]}


def test_one_visitor_on_many_pages_is_one_visitor(test_client) -> None:
    for page in ["/", "/uslugi", "/uslugi", "/ceny", "/"]:
        assert _event(test_client, "impression", page=page).status_code == 200
    _event(test_client, "impression", visitor=VISITOR_B, page="/")

    stages = _stages(_funnel(test_client))

    assert stages["Посетители с виджетом"]["count"] == 2
    assert stages["Посетители с виджетом"]["page_loads"] == 6


def test_chat_opened_counts_people_too(test_client) -> None:
    for _ in range(3):
        _event(test_client, "impression")
        _event(test_client, "chat-opened")

    assert _stages(_funnel(test_client))["Открыли чат"]["count"] == 1


def test_pages_breakdown_shows_where_chat_is_opened(test_client) -> None:
    for page in ["/uslugi/chistka", "/uslugi/chistka", "/", "/", "/"]:
        _event(test_client, "impression", page=page)
    _event(test_client, "chat-opened", page="/uslugi/chistka")

    pages = {row["page"]: row for row in _funnel(test_client)["pages"]}

    assert list(pages) == ["/", "/uslugi/chistka"]  # сортировка по загрузкам
    assert pages["/uslugi/chistka"] == {"page": "/uslugi/chistka", "loads": 2, "opens": 1, "open_rate": 50.0}
    assert pages["/"]["opens"] == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/uslugi/chistka-lica/?utm_source=yandex&phone=79991234567", "/uslugi/chistka-lica"),
        ("https://www.medcenterrosh.ru/ceny/#price", "/ceny"),
        ("/%D1%83%D1%81%D0%BB%D1%83%D0%B3%D0%B8", "/услуги"),
        ("/", "/"),
        ("", ""),
    ],
)
def test_page_is_path_only_without_query_or_hash(raw: str, expected: str) -> None:
    assert normalize_page(raw) == expected


def test_bots_are_not_counted(test_client, managed_env) -> None:
    response = test_client.post(
        EVENT_URL,
        json={"company_id": "rosh_demo", "kind": "impression", "visitor_id": VISITOR_A, "page": "/"},
        headers={"user-agent": "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)"},
    )

    assert response.status_code == 200
    assert _stages(_funnel(test_client))["Посетители с виджетом"]["count"] == 0


def test_bad_visitor_id_is_dropped_but_event_counts(test_client, managed_env) -> None:
    _event(test_client, "impression", visitor="<script>")

    rows = [json.loads(line) for line in managed_env["temp_dir"].joinpath("analytics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["event_type"] == "widget_impression"
    assert "visitor_id" not in rows[-1]["metadata"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"company_id": "rosh_demo", "kind": "clicked"}, 400),
        ({"company_id": "no_such_client", "kind": "impression"}, 404),
    ],
)
def test_unknown_kind_or_company_is_rejected(body: dict, status: int, test_client) -> None:
    assert test_client.post(EVENT_URL, json=body).status_code == status


def test_old_track_endpoints_still_work_for_cached_widgets(test_client) -> None:
    assert test_client.post("/api/analytics/track/impression", json={"company_id": "rosh_demo"}).status_code == 200
    assert test_client.post("/api/analytics/track/chat-opened", json={"company_id": "rosh_demo"}).status_code == 200

    stages = _stages(_funnel(test_client))
    assert stages["Посетители с виджетом"]["count"] == 1
    assert stages["Открыли чат"]["count"] == 1


def test_open_to_dialog_percent_only_when_all_opens_come_from_new_beacon(test_client) -> None:
    _event(test_client, "impression")
    _event(test_client, "chat-opened")
    first = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": None, "message": "сколько стоит чистка лица"})
    assert first.status_code == 200

    assert _stages(_funnel(test_client))["Есть переписка"]["percent_of_previous"] == 100.0

    test_client.post("/api/analytics/track/chat-opened", json={"company_id": "rosh_demo"})  # старый виджет из кеша

    assert _stages(_funnel(test_client))["Есть переписка"]["percent_of_previous"] is None


def test_widget_sends_events_to_the_new_path_with_visitor_and_page() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "widget" / "widget.js").read_text(encoding="utf-8")

    assert '"/api/widget/event"' in source
    assert "\"/api/analytics/track/\" + kind" not in source
    assert "visitor_id: this.visitorId()" in source
    assert "page: window.location.pathname" in source
