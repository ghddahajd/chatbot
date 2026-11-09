"""Опциональные интеграционные проверки на отдельном PostgreSQL-контуре."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

if not importlib.util.find_spec("sqlmodel") or os.environ.get("DB_INTEGRATION_TEST") != "1":
    raise unittest.SkipTest("Запускается отдельно в storage-контуре с DB_INTEGRATION_TEST=1")

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.connection import make_engine
from app.db.models import Tenant

BACKEND = Path(__file__).resolve().parents[1]


class DatabaseFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prefix = "probe" + uuid4().hex
        self.engine = make_engine()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        try:
            async with self.sessions() as session:
                await session.execute(delete(Tenant).where(Tenant.company_id.like(self.prefix + "%")))
                await session.commit()
        finally:
            await self.engine.dispose()

    async def records(self):
        async with self.sessions() as session:
            return list((await session.execute(select(Tenant).where(
                Tenant.company_id.like(self.prefix + "%")).order_by(Tenant.company_id))).scalars())

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(BACKEND / "scripts/db_admin.py"), *args],
                              capture_output=True, text=True, env=env)

    def write_client(self, root, suffix, name):
        cid = self.prefix + suffix
        folder = root / cid
        folder.mkdir()
        (folder / "company.yaml").write_text(json.dumps({
            "company_id": cid, "company_name": name, "city": "Казань",
            "working_hours": "Пн-Пт", "phone": "Тестовый контакт"}), encoding="utf-8")
        for filename in ("services.json", "prices.json"):
            (folder / filename).write_text("[]")
        (folder / "faq.md").write_text("")
        return folder

    async def test_unique_company_id_and_timezone(self):
        async with self.sessions() as session:
            session.add(Tenant(company_id=self.prefix, company_name="Проверка"))
            await session.commit()
        record = (await self.records())[0]
        self.assertIsNotNone(record.created_at.tzinfo)
        self.assertIsNotNone(record.updated_at.tzinfo)
        async with self.sessions() as session:
            session.add(Tenant(company_id=self.prefix, company_name="Дубликат"))
            with self.assertRaises(IntegrityError):
                await session.commit()
        self.assertEqual(len(await self.records()), 1)

    async def test_database_rejects_invalid_ids_and_blank_names(self):
        for cid, name in ((self.prefix + "/bad", "Название"), (self.prefix, "  ")):
            async with self.sessions() as session:
                session.add(Tenant(company_id=cid, company_name=name))
                with self.assertRaises(IntegrityError):
                    await session.commit()
        self.assertEqual(await self.records(), [])

    async def test_registration_is_repeatable_and_updates_only_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = self.write_client(root, "_a", "Клиника А")
            self.write_client(root, "_b", "Клиника Б")
            args = ("register-clients", "--clients-dir", str(root))
            preview = self.cli(*args, "--dry-run")
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(await self.records(), [])
            first = self.cli(*args)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = [(r.id, r.created_at, r.updated_at) for r in await self.records()]
            second = self.cli(*args)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(before, [(r.id, r.created_at, r.updated_at) for r in await self.records()])
            data = json.loads((a / "company.yaml").read_text())
            data["company_name"] = "Новое название А"
            (a / "company.yaml").write_text(json.dumps(data))
            self.assertEqual(self.cli(*args).returncode, 0)
            after = await self.records()
            self.assertEqual(after[0].company_name, "Новое название А")
            self.assertEqual(after[0].id, before[0][0])
            self.assertEqual(after[0].created_at, before[0][1])
            self.assertEqual(after[1].updated_at, before[1][2])

    async def test_registration_rolls_back_the_whole_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_client(root, "_a", "Клиника А")
            self.write_client(root, "_b", "X" * 256)
            result = self.cli("register-clients", "--clients-dir", str(root))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(await self.records(), [])

    async def test_missing_kb_does_not_delete_registry(self):
        async with self.sessions() as session:
            session.add(Tenant(company_id=self.prefix + "_old", company_name="Предыдущий клиент"))
            await session.commit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_client(root, "_new", "Новый клиент")
            result = self.cli("register-clients", "--clients-dir", str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(await self.records()), 2)

    async def test_connection_failure_has_no_secret_or_traceback(self):
        env = dict(os.environ, DATABASE_URL="invalid://probe-secret-marker@broken")
        result = self.cli("check", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("probe-secret-marker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        env = dict(os.environ, DATABASE_URL="", PGHOST="127.0.0.1", PGPORT="1")
        result = self.cli("check", env=env)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(env.get("PGPASSWORD", "not-present"), result.stderr)


if __name__ == "__main__":
    unittest.main()
