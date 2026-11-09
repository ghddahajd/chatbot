"""Создание и отзыв персональных доступов; пароли вводятся только скрыто."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def manage(args, password):
    from sqlalchemy import delete, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.connection import make_engine
    from app.db.models import ClientAccount, ClientLoginSession, Tenant
    from app.passwords import hash_password, normalize_login

    engine = make_engine()
    try:
        async with async_sessionmaker(engine)() as session:
            async with session.begin():
                if args.command == "grant-runtime":
                    # Идентификатор роли фиксирован; пароль цитирует PostgreSQL, не shell.
                    literal = await session.scalar(text("SELECT quote_literal(:password)"), {"password": password})
                    exists = await session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname='chatbot_client_runtime'"))
                    operation = "ALTER" if exists else "CREATE"
                    connection = await session.connection()
                    await connection.exec_driver_sql(f"{operation} ROLE chatbot_client_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {literal}")
                    await session.execute(text("GRANT USAGE ON SCHEMA public TO chatbot_client_runtime"))
                    await session.execute(text("GRANT SELECT ON tenants, client_accounts TO chatbot_client_runtime"))
                    await session.execute(text("GRANT SELECT, INSERT, DELETE ON client_login_sessions TO chatbot_client_runtime"))
                else:
                    login = normalize_login(args.login)
                    if args.command == "create":
                        tenant = await session.scalar(select(Tenant).where(Tenant.company_id == args.company_id))
                        if tenant is None:
                            raise ValueError("Сначала зарегистрируйте клинику")
                        session.add(ClientAccount(tenant_id=tenant.id, login=login, password_hash=hash_password(password)))
                    else:
                        account = await session.scalar(select(ClientAccount).where(ClientAccount.login == login))
                        if account is None:
                            raise ValueError("Учётная запись не найдена")
                        if args.command == "disable":
                            account.active = False
                        else:
                            account.password_hash = hash_password(password)
                        # Смена версии также отзывает вход, начатый до сброса пароля и завершившийся после него.
                        account.auth_version = uuid4()
                        await session.execute(delete(ClientLoginSession).where(ClientLoginSession.account_id == account.id))
        print("Доступ обновлён; секреты не выводятся")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "disable", "reset-password", "grant-runtime"])
    parser.add_argument("--login")
    parser.add_argument("--company-id")
    args = parser.parse_args()
    if args.command != "grant-runtime" and not args.login:
        parser.error("Требуется --login")
    if args.command == "create" and not args.company_id:
        parser.error("Требуется --company-id")
    try:
        password = None
        if args.command != "disable":
            if not sys.stdin.isatty():
                raise ValueError("Для ввода пароля нужен интерактивный терминал")
            password = getpass.getpass("Новый пароль: ")
            if password != getpass.getpass("Повторите пароль: "):
                raise ValueError("Пароли не совпадают")
            from app.passwords import hash_password
            hash_password(password)
        asyncio.run(manage(args, password))
    except Exception as error:
        print(f"Не удалось изменить доступ ({type(error).__name__}). Проверьте параметры, миграции и права владельца БД.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
