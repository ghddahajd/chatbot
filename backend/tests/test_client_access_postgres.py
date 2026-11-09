"""Настоящие PostgreSQL-сессии и HTTP-доступ сотрудников на временных клиниках."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not importlib.util.find_spec("sqlmodel") or os.environ.get("DB_INTEGRATION_TEST") != "1":
    raise unittest.SkipTest("Отдельный PostgreSQL-контур с DB_INTEGRATION_TEST=1")

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import CLIENT_COOKIE_NAME
from app.db.models import ClientAccount, ClientLoginSession, Tenant
from app.main import app
from app.passwords import hash_password

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "local-test-password-123"


async def owner_work(callback):
    engine = create_async_engine(make_url(os.environ["TEST_OWNER_DATABASE_URL"]), hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await callback(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


class ClientAccessPostgresTests(unittest.TestCase):
    def setUp(self):
        self.login_name = "staff_" + uuid4().hex
        async def seed(session):
            tenant = await session.scalar(select(Tenant).where(Tenant.company_id == "clinic_alpha"))
            account = ClientAccount(tenant_id=tenant.id, login=self.login_name, password_hash=hash_password(PASSWORD))
            session.add(account)
            return account.id
        self.account_id = asyncio.run(owner_work(seed))
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        archive = Path(os.environ["CONVERSATIONS_ARCHIVE_FILE"])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text("\n".join(json.dumps({"session_id": sid, "company_id": cid,
                            "status": "CLOSED", "closed_at": "2026-11-09T12:00:00",
                            "messages": [{"role": "user", "text": cid}]})
                            for sid, cid in [("a_archived", "clinic_alpha"), ("b_archived", "clinic_beta")]))

    def tearDown(self):
        self.context.__exit__(None, None, None)
        async def remove(session):
            await session.execute(delete(ClientLoginSession).where(ClientLoginSession.account_id == self.account_id))
            await session.execute(delete(ClientAccount).where(ClientAccount.id == self.account_id))
        asyncio.run(owner_work(remove))

    def login(self):
        result = self.client.post("/login", data={"username": self.login_name, "password": PASSWORD},
                                  headers=ORIGIN, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        return self.client.cookies.get(CLIENT_COOKIE_NAME)

    def test_real_login_scopes_all_collections_and_export(self):
        token = self.login()
        for endpoint in ("summary", "operators", "chats", "dashboard", "leads"):
            self.assertEqual(self.client.get("/api/analytics/" + endpoint).status_code, 200)
            self.assertEqual(self.client.get("/api/analytics/" + endpoint + "?company_id=clinic_beta").status_code, 403)
        self.assertEqual(self.client.get("/api/analytics/chats/a_archived").status_code, 200)
        self.assertEqual(self.client.get("/api/analytics/chats/b_archived").status_code, 404)
        data = self.client.post("/api/analytics/chats/export", json={"session_ids": ["a_archived", "b_archived"]}, headers=ORIGIN).json()
        self.assertEqual([row["session_id"] for row in data["conversations"]], ["a_archived"])
        async def stored(session):
            return list((await session.execute(select(ClientLoginSession).where(ClientLoginSession.account_id == self.account_id))).scalars())
        records = asyncio.run(owner_work(stored))
        self.assertEqual(records[0].token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(records[0].token_hash, token)
        self.assertEqual(self.client.get("/backstage").status_code, 403)

    def test_logout_revokes_token_in_database(self):
        token = self.login()
        self.assertEqual(self.client.post("/logout", headers=ORIGIN, follow_redirects=False).status_code, 303)
        self.client.cookies.set(CLIENT_COOKIE_NAME, token)
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)

    def test_expiry_and_account_disable_are_checked_each_request(self):
        self.login()
        async def expire(session):
            await session.execute(update(ClientLoginSession).where(ClientLoginSession.account_id == self.account_id)
                                  .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        asyncio.run(owner_work(expire))
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)
        self.login()
        async def disable(session):
            await session.execute(update(ClientAccount).where(ClientAccount.id == self.account_id).values(active=False))
        asyncio.run(owner_work(disable))
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)

    def test_admin_password_reset_revokes_existing_login(self):
        self.login()
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from client_accounts import manage
        with patch.dict(os.environ, {"DATABASE_URL": os.environ["TEST_OWNER_DATABASE_URL"]}):
            asyncio.run(manage(argparse.Namespace(command="reset-password", login=self.login_name), "new-local-password-123"))
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)
        wrong = self.client.post("/login", data={"username": self.login_name, "password": PASSWORD}, headers=ORIGIN)
        self.assertEqual(wrong.status_code, 401)

    def test_restricted_runtime_role_and_wrong_credentials(self):
        self.assertEqual(self.client.post("/login", data={"username": self.login_name, "password": "wrong"}, headers=ORIGIN).status_code, 401)
        self.assertEqual(self.client.post("/login", data={"username": "unknown", "password": "wrong"}, headers=ORIGIN).status_code, 401)
        async def restricted():
            from app.db.connection import make_engine
            engine = make_engine()
            try:
                async with engine.begin() as conn:
                    from sqlalchemy import text
                    self.assertFalse(await conn.scalar(text("SELECT has_table_privilege(current_user,'client_accounts','UPDATE')")))
                    self.assertFalse(await conn.scalar(text("SELECT has_table_privilege(current_user,'tenants','DELETE')")))
                    self.assertFalse(await conn.scalar(text("SELECT has_schema_privilege(current_user,'public','CREATE')")))
            finally:
                await engine.dispose()
        asyncio.run(restricted())

    def test_password_reset_during_login_cannot_create_valid_old_session(self):
        from app.passwords import verify_password
        def racing_reset(password, encoded):
            async def reset(session):
                await session.execute(update(ClientAccount).where(ClientAccount.id == self.account_id)
                                      .values(auth_version=uuid4()))
            asyncio.run(owner_work(reset))
            return verify_password(password, encoded)
        with patch("app.services.client_access.verify_password", side_effect=racing_reset):
            self.login()
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)

    def test_database_down_rejects_client_but_not_owner_or_chat(self):
        self.login()
        from app.services.client_access import ClientAccessService
        previous = app.state.client_access
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+asyncpg://unused:unused@127.0.0.1:1/unavailable"}):
            app.state.client_access = ClientAccessService(24)
        self.context.portal.call(previous.close)
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 503)
        owner = {"X-Operator-Token": os.environ["OPERATOR_TOKEN"]}
        self.assertEqual(self.client.get("/api/analytics/chats", headers=owner).status_code, 200)
        self.assertEqual(self.client.post("/api/chat/message", json={"company_id": "clinic_alpha", "message": "Привет"}).status_code, 200)

    def test_owner_database_role_cannot_serve_client_login(self):
        from app.services.client_access import ClientAccessService
        previous = app.state.client_access
        with patch.dict(os.environ, {"DATABASE_URL": os.environ["TEST_OWNER_DATABASE_URL"]}):
            app.state.client_access = ClientAccessService(24)
        self.context.portal.call(previous.close)
        result = self.client.post("/login", data={"username": self.login_name, "password": PASSWORD}, headers=ORIGIN)
        self.assertEqual(result.status_code, 503)

    def test_admin_create_and_disable_only_selected_employee(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from client_accounts import manage
        other = "staff_" + uuid4().hex
        async def remove_other(session):
            account = await session.scalar(select(ClientAccount).where(ClientAccount.login == other))
            if account:
                await session.execute(delete(ClientLoginSession).where(ClientLoginSession.account_id == account.id))
                await session.delete(account)
        self.addCleanup(lambda: asyncio.run(owner_work(remove_other)))
        self.login()
        with patch.dict(os.environ, {"DATABASE_URL": os.environ["TEST_OWNER_DATABASE_URL"]}):
            asyncio.run(manage(argparse.Namespace(command="create", login=other, company_id="clinic_beta"), PASSWORD))
            asyncio.run(manage(argparse.Namespace(command="disable", login=self.login_name), None))
        self.assertEqual(self.client.get("/api/analytics/chats").status_code, 403)
        result = self.client.post("/login", data={"username": other, "password": PASSWORD}, headers=ORIGIN, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.client.get("/api/settings/company?company_id=clinic_beta").status_code, 200)
        self.assertEqual(self.client.get("/api/settings/company?company_id=clinic_alpha").status_code, 403)


if __name__ == "__main__":
    unittest.main()
