"""Миграции без импорта FastAPI и запуска его фоновых задач."""

import asyncio

from alembic import context
from sqlmodel import SQLModel

from app.db.connection import database_url, make_engine
from app.db import models  # noqa: F401


def migrate(connection):
    context.configure(connection=connection, target_metadata=SQLModel.metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = make_engine()
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=SQLModel.metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
