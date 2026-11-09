"""HTTP-изоляция клиник без обязательного PostgreSQL в обычном прогоне."""

import json

import pytest

from app.auth import CLIENT_COOKIE_NAME, Principal
from app.passwords import hash_password, normalize_login, verify_password

ORIGIN = {"Origin": "http://testserver"}


class FakeAccess:
    def __init__(self):
        self.tokens = {}

    async def login(self, login, password):
        if login not in {"staff_a", "staff_b"} or password != "test-only-password":
            return None
        token = "local-test-session-" + login
        cid = "dup_one" if login == "staff_a" else "dup_two"
        self.tokens[token] = Principal(owner=False, company_id=cid, account_id=login)
        return token

    async def authenticate(self, token):
        return self.tokens.get(token)

    async def logout(self, token):
        self.tokens.pop(token, None)

    async def close(self):
        pass


@pytest.fixture()
def staff_client(test_client, monkeypatch, managed_env):
    from app import main
    monkeypatch.setattr(main.settings, "client_login_enabled", True)
    monkeypatch.setattr(test_client.app.state.settings, "client_login_enabled", True)
    service = FakeAccess()
    test_client.app.state.client_access = service
    archive = managed_env["temp_dir"] / "access-archive.jsonl"
    archive.write_text("\n".join(json.dumps({"session_id": sid, "company_id": cid,
                          "status": "CLOSED", "messages": [{"role": "user", "text": cid}],
                          "closed_at": "2026-11-09T12:00:00"})
                          for sid, cid in [("archive_a", "dup_one"), ("archive_b", "dup_two")]))
    monkeypatch.setattr(test_client.app.state.analytics_service, "conversations_archive_file", archive)
    response = test_client.post("/login", data={"username": "staff_a", "password": "test-only-password",
                                "next": "/backstage"}, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/analytics"
    assert "HttpOnly" in response.headers["set-cookie"]
    return test_client


def test_password_hash_and_normalization():
    password = "Пароль для теста 123!"
    encoded = hash_password(password)
    assert encoded != hash_password(password)
    assert password not in encoded
    assert verify_password(password, encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password(password, "broken")
    assert normalize_login(" Staff.A ") == "staff.a"
    with pytest.raises(ValueError):
        normalize_login("../staff")


@pytest.mark.parametrize("endpoint", ["summary", "operators", "dashboard", "leads", "chats"])
def test_collections_bind_to_own_clinic(staff_client, endpoint):
    response = staff_client.get("/api/analytics/" + endpoint)
    assert response.status_code == 200
    if endpoint == "chats":
        assert all(row["company_id"] == "dup_one" for row in response.json()["conversations"])
    elif endpoint != "operators":
        assert response.json().get("company_id") == "dup_one"
    assert staff_client.get("/api/analytics/" + endpoint + "?company_id=dup_two").status_code == 403


def test_detail_and_mixed_export_do_not_leak_other_clinic(staff_client):
    assert staff_client.get("/api/analytics/chats/archive_a").status_code == 200
    assert staff_client.get("/api/analytics/chats/archive_b").status_code == 404
    export = staff_client.post("/api/analytics/chats/export", json={"session_ids": ["archive_a", "archive_b"]}, headers=ORIGIN)
    assert export.status_code == 200
    assert export.json()["company_id"] == "dup_one"
    assert [row["session_id"] for row in export.json()["conversations"]] == ["archive_a"]
    assert staff_client.post("/api/analytics/chats/export", json={"company_id": "dup_two"}, headers=ORIGIN).status_code == 403


def test_settings_scope_and_csrf(staff_client, managed_env):
    own = "/api/settings/company?company_id=dup_one"
    foreign = "/api/settings/company?company_id=dup_two"
    assert staff_client.get(own).status_code == 200
    assert staff_client.get(foreign).status_code == 403
    data = staff_client.get(own).json()
    payload = {"phone": "Тестовый контакт", "address": "Свой адрес", "widget": {
               key: data["widget"][key] for key in ("primary_color", "button_color", "header_title")},
               "working_hours_schedule": {}, "facts": {}}
    assert staff_client.post(own, json=payload).status_code == 403
    assert staff_client.post(own, json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
    assert staff_client.post(own, json=payload, headers=ORIGIN).status_code == 200
    assert staff_client.post(foreign, json=payload, headers=ORIGIN).status_code == 403
    assert staff_client.post("/api/settings/company/reset-block?company_id=dup_two&block=widget", headers=ORIGIN).status_code == 403
    assert not (managed_env["temp_dir"] / "overrides/dup_two.json").exists()


@pytest.mark.parametrize("method,path", [("GET", "/backstage"), ("GET", "/operator"),
    ("GET", "/api/operator/sessions"), ("GET", "/api/operator/sessions/archive_a"),
    ("POST", "/api/operator/sessions/archive_b/take"), ("POST", "/api/operator/sessions/archive_b/close"),
    ("GET", "/debug"), ("GET", "/debug/rag"), ("GET", "/api/debug/preflight"),
    ("GET", "/api/debug/telegram-check"), ("POST", "/api/debug/trace"),
    ("GET", "/api/delivery/outbox"), ("POST", "/api/delivery/retry")])
def test_service_routes_remain_owner_only(staff_client, method, path):
    response = staff_client.request(method, path, headers=ORIGIN,
                                   json={"company_id": "dup_one", "message": "Привет"} if method == "POST" else None)
    assert response.status_code == 403


def test_page_logout_and_cookie_replay(staff_client):
    page = staff_client.get("/analytics")
    assert page.status_code == 200
    assert '<option value="dup_one" selected>' in page.text
    assert '<option value="dup_two"' not in page.text
    old_token = staff_client.cookies.get(CLIENT_COOKIE_NAME)
    assert staff_client.post("/logout").status_code == 403
    assert staff_client.post("/logout", headers=ORIGIN, follow_redirects=False).status_code == 303
    staff_client.cookies.set(CLIENT_COOKIE_NAME, old_token)
    assert staff_client.get("/api/analytics/chats").status_code == 403


def test_database_failure_is_closed_but_owner_and_widget_work(staff_client, monkeypatch):
    async def fail(*args):
        raise RuntimeError("do-not-disclose-db-url")
    monkeypatch.setattr(staff_client.app.state.client_access, "authenticate", fail)
    response = staff_client.get("/api/analytics/chats")
    assert response.status_code == 503
    assert "do-not-disclose" not in response.text
    assert staff_client.get("/api/analytics/chats", headers={"X-Operator-Token": "demo-operator-token"}).status_code == 200
    assert staff_client.post("/api/chat/message", json={"company_id": "dup_one", "message": "Привет"}).status_code == 200


def test_login_origin_and_limit(test_client, monkeypatch):
    from app import main
    monkeypatch.setattr(main.settings, "client_login_enabled", True)
    assert test_client.post("/login", data={"password": "wrong"}).status_code == 403
    responses = [test_client.post("/login", data={"password": "wrong"}, headers=ORIGIN) for _ in range(11)]
    assert responses[-1].status_code == 429


def test_client_cookie_cannot_authorize_operator_websocket(staff_client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as error:
        with staff_client.websocket_connect("/ws/operator?session_id=archive_a"):
            pass
    assert error.value.code == 4403


def test_staff_login_cookie_is_secure_over_https(staff_client):
    result = staff_client.post("/login", data={"username": "staff_a", "password": "test-only-password"},
                               headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert result.status_code == 303
    assert "Secure" in result.headers["set-cookie"]
