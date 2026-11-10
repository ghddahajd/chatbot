"""Реестр клиентов; цены, домены и настройки по-прежнему берутся из KB."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, UniqueConstraint, func
from sqlmodel import Field, SQLModel


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("company_id", name="uq_tenants_company_id"),
        CheckConstraint("company_id ~ '^[A-Za-z0-9_-]+$'", name="ck_tenants_company_id"),
        CheckConstraint("length(trim(company_name)) > 0", name="ck_tenants_company_name"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    company_id: str = Field(max_length=64)
    company_name: str = Field(max_length=255)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc),
                                 sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now(), index=True))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc),
                                 sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now()))


class ClientAccount(SQLModel, table=True):
    __tablename__ = "client_accounts"
    __table_args__ = (
        UniqueConstraint("login", name="uq_client_accounts_login"),
        CheckConstraint("login ~ '^[a-z0-9][a-z0-9_.@-]{2,63}$'", name="ck_client_accounts_login"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenants.id", nullable=False, index=True)
    login: str = Field(max_length=64)
    password_hash: str = Field(max_length=255)
    auth_version: UUID = Field(default_factory=uuid4)
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc),
                                 sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now()))


class ClientLoginSession(SQLModel, table=True):
    __tablename__ = "client_login_sessions"

    token_hash: str = Field(max_length=64, primary_key=True)
    account_id: UUID = Field(foreign_key="client_accounts.id", nullable=False, index=True)
    auth_version: UUID
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc),
                                 sa_column=Column(DateTime(timezone=True), nullable=False,
                                                  server_default=func.now()))


# Alembic must see the opt-in storage tables even when runtime storage stays file-based.
from . import lead_models  # noqa: E402, F401
from . import request_models  # noqa: E402, F401
