"""Grant the fixed, isolated lead runtime role; never activates PostgreSQL storage."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def grant_runtime(password):
    from sqlalchemy import text
    from app.db.connection import make_engine

    engine = make_engine()
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='chatbot_lead_runtime'"))
            if exists:
                unsafe = await connection.scalar(text("""
                    SELECT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r
                                   ON r.oid=m.member WHERE r.rolname='chatbot_lead_runtime')
                       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
                                  WHERE r.rolname='chatbot_lead_runtime')
                       OR EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
                                  WHERE r.rolname='chatbot_lead_runtime')
                       OR EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba
                                  WHERE r.rolname='chatbot_lead_runtime')
                """))
                if unsafe:
                    raise ValueError("Existing runtime role owns objects or belongs to another role")
            # PostgreSQL quotes the secret, and SQLAlchemy hides parameters and SQL errors at the CLI.
            literal = await connection.scalar(text("SELECT quote_literal(:password)"), {"password": password})
            operation = "ALTER" if exists else "CREATE"
            await connection.exec_driver_sql(
                f"{operation} ROLE chatbot_lead_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"NOINHERIT NOBYPASSRLS NOREPLICATION PASSWORD {literal}")
            await connection.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM chatbot_lead_runtime"))
            await connection.execute(text("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM chatbot_lead_runtime"))
            await connection.execute(text("REVOKE ALL ON SCHEMA public FROM chatbot_lead_runtime"))
            await connection.execute(text("GRANT USAGE ON SCHEMA public TO chatbot_lead_runtime"))
            await connection.execute(text("GRANT SELECT ON public.tenants TO chatbot_lead_runtime"))
            await connection.execute(text("GRANT SELECT, INSERT ON public.leads TO chatbot_lead_runtime"))
            await connection.execute(text("GRANT SELECT, INSERT, UPDATE ON public.delivery_outbox TO chatbot_lead_runtime"))
            await connection.execute(text("GRANT SELECT, INSERT, UPDATE ON public.chat_requests TO chatbot_lead_runtime"))
    finally:
        await engine.dispose()


async def manage(args, password):
    if args.command != "grant-runtime" or not isinstance(password, str) or not password:
        raise ValueError("Expected grant-runtime and a nonempty password")
    try:
        await grant_runtime(password)
    except Exception as error:
        # DDL errors can embed the quoted password in SQL, even with hide_parameters.
        raise RuntimeError(f"Lead runtime grant failed ({type(error).__name__})") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["grant-runtime"])
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise ValueError("Interactive terminal required")
        password = getpass.getpass("Пароль chatbot_lead_runtime: ")
        if not password or password != getpass.getpass("Повторите пароль: "):
            raise ValueError("Passwords must match and be nonempty")
        asyncio.run(manage(args, password))
        print("Права роли выдавались только для tenants, leads, delivery_outbox и chat_requests; проверьте initialize().")
    except Exception as error:
        print(f"Операция не выполнена ({type(error).__name__}). Проверьте миграции и права владельца БД.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
