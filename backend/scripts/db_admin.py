"""Явная проверка БД и регистрация клиентских KB; runtime не переключает."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def read_clients(clients_dir=None):
    from app.config import get_settings
    from app.knowledge import KnowledgeBaseResolver

    settings = get_settings()
    resolver = KnowledgeBaseResolver(data_dir=settings.data_dir,
                                    clients_data_dir=clients_dir or settings.clients_data_dir,
                                    defaults_data_dir=settings.defaults_data_dir,
                                    default_company_id=settings.default_company_id)
    clients = []
    for cid in resolver.company_ids():
        company = resolver.get(cid, fallback=False).company
        if company.company_id != cid:
            raise ValueError("company_id не совпадает с именем клиентской папки")
        clients.append({"company_id": cid, "company_name": company.company_name})
    if not clients:
        raise ValueError("Нет зарегистрированных клиентских KB")
    return clients


async def run(command, clients):
    from sqlalchemy import func, text
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.connection import make_engine
    from app.db.models import Tenant

    engine = make_engine()
    try:
        if command == "check":
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                if revision != "0001_client_registry":
                    raise ValueError("Примените миграции до текущей версии")
            print("БД доступна, миграция реестра применена")
        else:
            async with async_sessionmaker(engine)() as session:
                async with session.begin():
                    for client in clients:
                        statement = insert(Tenant).values(**client)
                        statement = statement.on_conflict_do_update(
                            constraint="uq_tenants_company_id",
                            set_={"company_name": statement.excluded.company_name, "updated_at": func.now()},
                            where=Tenant.company_name != statement.excluded.company_name)
                        await session.execute(statement)
            print(f"Клиентов обработано: {len(clients)}; отсутствующие в KB записи не удалялись")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "register-clients"])
    parser.add_argument("--clients-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run and args.command != "register-clients":
            raise ValueError("--dry-run применим только к register-clients")
        clients = read_clients(args.clients_dir) if args.command == "register-clients" else []
        if args.dry_run:
            print(f"Клиентов к регистрации: {len(clients)}; БД не изменялась")
        else:
            asyncio.run(run(args.command, clients))
    except Exception as error:
        # Драйвер может включать DSN в текст ошибки: выводим только класс и безопасную подсказку.
        print(f"Операция БД не выполнена ({type(error).__name__}). Проверьте настройки, миграции и requirements-db.txt.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
