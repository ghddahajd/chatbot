"""Сессии входа сотрудников; история чатов и заявки остаются в прежних хранилищах."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..auth import Principal
from ..db.connection import make_engine
from ..db.models import ClientAccount, ClientLoginSession, Tenant
from ..passwords import hash_password, normalize_login, verify_password

DUMMY_HASH = hash_password("unused-dummy-password-for-timing")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ClientAccessService:
    def __init__(self, hours: int):
        self.engine = make_engine()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.hours = hours
        self.password_slots = asyncio.Semaphore(4)

    async def _verify_role(self, session):
        # Даже ошибочно переданный owner-DSN не должен обслуживать клиентский вход.
        limited = await session.scalar(text("SELECT current_user = 'chatbot_client_runtime' "
            "AND NOT has_table_privilege(current_user, 'client_accounts', 'UPDATE') "
            "AND NOT has_table_privilege(current_user, 'tenants', 'DELETE') "
            "AND NOT has_schema_privilege(current_user, 'public', 'CREATE')"))
        if not limited:
            raise RuntimeError("Требуется ограниченная runtime-роль")

    async def login(self, login: str, password: str):
        try:
            login = normalize_login(login)
        except ValueError:
            login = ""
        async with self.sessions() as session:
            await self._verify_role(session)
            account = await session.scalar(select(ClientAccount).where(ClientAccount.login == login))
        async with self.password_slots:
            valid = await asyncio.to_thread(verify_password, password, account.password_hash if account else DUMMY_HASH)
        if not account or not valid or not account.active:
            return None
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(delete(ClientLoginSession).where(ClientLoginSession.expires_at <= now))
                session.add(ClientLoginSession(token_hash=token_hash(token), account_id=account.id,
                                               auth_version=account.auth_version,
                                               expires_at=now + timedelta(hours=self.hours)))
        return token

    async def authenticate(self, token: str):
        if not 32 <= len(token) <= 128:
            return None
        async with self.sessions() as session:
            await self._verify_role(session)
            row = (await session.execute(select(ClientAccount.id, Tenant.company_id)
                   .join(ClientLoginSession, ClientLoginSession.account_id == ClientAccount.id)
                   .join(Tenant, Tenant.id == ClientAccount.tenant_id)
                   .where(ClientLoginSession.token_hash == token_hash(token), ClientAccount.active.is_(True),
                          ClientLoginSession.auth_version == ClientAccount.auth_version,
                          ClientLoginSession.expires_at > datetime.now(timezone.utc)))).first()
        return Principal(owner=False, company_id=row.company_id, account_id=str(row.id)) if row else None

    async def logout(self, token: str):
        async with self.sessions() as session:
            await self._verify_role(session)
            await session.execute(delete(ClientLoginSession).where(ClientLoginSession.token_hash == token_hash(token)))
            await session.commit()

    async def close(self):
        await self.engine.dispose()
