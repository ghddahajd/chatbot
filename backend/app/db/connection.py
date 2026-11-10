"""Явное подключение для инструментов БД, без неявного запуска из приложения."""

import os

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine


def database_url(prefix: str = "") -> URL:
    raw = os.environ.get(prefix + "DATABASE_URL", "").strip()
    if raw:
        try:
            url = make_url(raw)
        except Exception:
            raise ValueError("Некорректный DATABASE_URL") from None
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL должен использовать postgresql+asyncpg")
        return url
    required = tuple(prefix + name for name in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"))
    if not all(os.environ.get(name) for name in required):
        raise ValueError("Задайте DATABASE_URL либо PGHOST, PGUSER, PGPASSWORD, PGDATABASE")
    try:
        port = int(os.environ.get(prefix + "PGPORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("Некорректный PGPORT") from None
    return URL.create("postgresql+asyncpg", host=os.environ[prefix + "PGHOST"], port=port,
                      username=os.environ[prefix + "PGUSER"], password=os.environ[prefix + "PGPASSWORD"],
                      database=os.environ[prefix + "PGDATABASE"])


def make_engine(prefix: str = ""):
    return create_async_engine(database_url(prefix), pool_size=5, max_overflow=0,
                               pool_timeout=5, pool_pre_ping=True, hide_parameters=True,
                               connect_args={"timeout": 5, "command_timeout": 15})
