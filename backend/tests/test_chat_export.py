"""выгрузка диалогов из вкладки "Чаты" (/backstage) — POST /api/analytics/chats/export.

Проверяем доступ, маскировку телефонов, белый список полей, живые и архивные диалоги,
выбор по id и по фильтру, ограничение по компании и то, что на клиентской /analytics
кнопок выгрузки нет."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

HEADERS = {"X-Operator-Token": "demo-operator-token"}
EXPORT_URL = "/api/analytics/chats/export"


@pytest.fixture()
def archive_file(test_client, tmp_path: Path) -> Path:
    """своя копия архива диалогов: по умолчанию он указывает на настоящий backend/logs."""

    path = tmp_path / "conversations_archive.jsonl"
    test_client.app.state.analytics_service.conversations_archive_file = path
    return path


def _archive(path: Path, **record) -> None:
    base = {
        "session_id": "arch-0001",
        "company_id": "rosh_demo",
        "status": "CLOSED",
        "lead_requested": False,
        "operator_requested": True,
        "telegram_claimed_by": "@secret_operator",
        "created_at": "2026-09-20T10:00:00",
        "closed_at": "2026-09-20T10:05:00",
        "messages": [
            {"role": "user", "text": "перезвоните на 8 (926) 555-44-33", "kind": None, "created_at": "2026-09-20T10:00:00"},
            {"role": "assistant", "text": "Передала менеджеру", "kind": None, "created_at": "2026-09-20T10:00:01"},
        ],
    }
    base.update(record)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(base, ensure_ascii=False) + "\n")


def _chat(client, message: str, session_id: str | None = None) -> str:
    response = client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": message},
    )
    assert response.status_code == 200
    return response.json()["session_id"]


def _export(client, **body) -> dict:
    response = client.post(EXPORT_URL, json=body, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_export_requires_operator_token(test_client) -> None:
    response = test_client.post(EXPORT_URL, json={})

    assert response.status_code == 403


def test_export_live_session_by_id_masks_phones(test_client, archive_file) -> None:
    session_id = _chat(test_client, "здравствуйте")
    _chat(test_client, "запишите меня, телефон +7 926 555-44-33", session_id)

    data = _export(test_client, session_ids=[session_id])

    assert data["count"] == 1 and data["phones_masked"] is True
    chat = data["conversations"][0]
    assert chat["session_id"] == session_id and chat["source"] == "live"
    texts = [message["text"] for message in chat["messages"]]
    assert any("<phone>" in text for text in texts)
    assert not any("555-44-33" in text or "926 555" in text for text in texts)
    assert {message["role"] for message in chat["messages"]} >= {"user", "assistant"}


def test_export_archived_record_uses_whitelisted_fields(test_client, archive_file) -> None:
    _archive(archive_file)

    chat = _export(test_client, session_ids=["arch-0001"])["conversations"][0]

    assert chat["source"] == "archive"
    assert chat["updated_at"] == "2026-09-20T10:05:00"
    assert "telegram_claimed_by" not in chat
    assert "secret_operator" not in json.dumps(chat, ensure_ascii=False)
    assert chat["messages"][0]["text"] == "перезвоните на <phone>"
    assert set(chat) == {
        "session_id", "company_id", "status", "operator_requested", "lead_requested",
        "created_at", "updated_at", "source", "messages",
    }


def test_export_prefers_the_fuller_duplicate_in_archive(test_client, archive_file) -> None:
    _archive(archive_file, messages=[{"role": "user", "text": "первая", "created_at": "2026-09-20T10:00:00"}])
    _archive(archive_file)  # тот же session_id, два сообщения

    chat = _export(test_client, session_ids=["arch-0001"])["conversations"][0]

    assert len(chat["messages"]) == 2


def test_export_all_by_filter_includes_live_and_archive(test_client, archive_file) -> None:
    _archive(archive_file, session_id="arch-op", operator_requested=True)
    _archive(archive_file, session_id="arch-bot", operator_requested=False)
    live_id = _chat(test_client, "какой у вас адрес")

    everything = {c["session_id"] for c in _export(test_client, scope="all")["conversations"]}
    operator_only = {c["session_id"] for c in _export(test_client, scope="operator")["conversations"]}

    assert {"arch-op", "arch-bot", live_id} <= everything
    assert "arch-op" in operator_only and "arch-bot" not in operator_only


def test_export_by_ids_keeps_order_skips_unknown_and_duplicates(test_client, archive_file) -> None:
    _archive(archive_file, session_id="a1")
    _archive(archive_file, session_id="a2")

    data = _export(test_client, session_ids=["a2", "missing", "a1", "a2"])

    assert [c["session_id"] for c in data["conversations"]] == ["a2", "a1"]
    assert data["count"] == 2


def test_export_respects_company_filter_even_for_explicit_ids(test_client, archive_file) -> None:
    _archive(archive_file, session_id="own", company_id="rosh_demo")
    _archive(archive_file, session_id="foreign", company_id="dup_one")

    data = _export(test_client, company_id="rosh_demo", session_ids=["own", "foreign"])

    assert [c["session_id"] for c in data["conversations"]] == ["own"]


def test_export_limit(test_client, archive_file) -> None:
    for index in range(5):
        _archive(archive_file, session_id=f"s{index}", closed_at=f"2026-09-20T10:0{index}:00")

    assert _export(test_client, limit=3)["count"] == 3
    assert _export(test_client, session_ids=[f"s{i}" for i in range(5)], limit=2)["count"] == 2


def test_export_rejects_out_of_range_limit(test_client) -> None:
    assert test_client.post(EXPORT_URL, json={"limit": 0}, headers=HEADERS).status_code == 422
    assert test_client.post(EXPORT_URL, json={"limit": 501}, headers=HEADERS).status_code == 422


def test_export_response_is_a_download(test_client, archive_file) -> None:
    response = test_client.post(EXPORT_URL, json={}, headers=HEADERS)

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="chats_')


def test_export_is_logged_without_dialog_text(test_client, archive_file, caplog: pytest.LogCaptureFixture) -> None:
    _archive(archive_file)

    with caplog.at_level(logging.INFO, logger="app"):
        _export(test_client, session_ids=["arch-0001"])

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("chats_exported"))
    assert "count=1" in line and "selected=1" in line
    assert "перезвоните" not in line and "926" not in line


def test_backstage_has_export_controls_and_client_page_does_not(test_client) -> None:
    backstage = test_client.get("/backstage?token=demo-operator-token").text
    client_page = test_client.get("/analytics?token=demo-operator-token").text

    assert 'id="exportAllBtn"' in backstage and 'id="exportSelectedBtn"' in backstage
    assert "const CHAT_EXPORT_ENABLED = true;" in backstage
    assert 'id="exportAllBtn"' not in client_page and 'id="exportSelectedBtn"' not in client_page
    assert "const CHAT_EXPORT_ENABLED = false;" in client_page
    for page in (backstage, client_page):
        assert "__CHAT_EXPORT" not in page
