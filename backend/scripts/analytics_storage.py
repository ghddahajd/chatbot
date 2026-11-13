"""Provision the isolated analytics role without enabling synchronization."""

import argparse
import asyncio
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def grant_runtime(password):
    from sqlalchemy import text
    from app.db.connection import make_engine

    engine = make_engine()
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(text("""
                SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chatbot_analytics_runtime')
            """))
            if exists:
                raise ValueError("Role already exists")
            literal = await connection.scalar(text("SELECT quote_literal(:password)"), {"password": password})
            await connection.exec_driver_sql(
                "CREATE ROLE chatbot_analytics_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"NOINHERIT NOBYPASSRLS NOREPLICATION PASSWORD {literal}"
            )
            for statement in (
                "GRANT USAGE ON SCHEMA public TO chatbot_analytics_runtime",
                "GRANT SELECT ON public.tenants TO chatbot_analytics_runtime",
                "GRANT SELECT, INSERT, UPDATE ON public.analytics_events TO chatbot_analytics_runtime",
                "GRANT SELECT, INSERT, DELETE ON public.analytics_rollup TO chatbot_analytics_runtime",
            ):
                await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["grant-runtime"])
    parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise ValueError("Interactive terminal required")
        password = getpass.getpass("Пароль chatbot_analytics_runtime: ")
        if not password or password != getpass.getpass("Повторите пароль: "):
            raise ValueError("Passwords must match and be nonempty")
        asyncio.run(grant_runtime(password))
    except Exception as error:
        print(f"Операция не выполнена ({type(error).__name__}). Проверьте миграции и наличие роли.", file=sys.stderr)
        return 1
    print("Роль chatbot_analytics_runtime создана")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
