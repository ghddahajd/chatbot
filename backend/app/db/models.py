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
