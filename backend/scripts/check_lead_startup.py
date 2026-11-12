"""Run backend startup and shutdown with PostgreSQL, without HTTP or deliveries."""

import argparse
import asyncio
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def idle_delivery_worker(self, interval_seconds):
    await asyncio.Future()


async def check_startup():
    from app.config import get_settings
    from app.main import app
    from app.services.postgres_delivery import PostgresDeliveryService

    if get_settings().lead_storage_backend != "postgres":
        raise RuntimeError("Smoke check requires the PostgreSQL configuration")
    PostgresDeliveryService.run_retry_loop = idle_delivery_worker
    async with app.router.lifespan_context(app):
        health = await app.state.delivery_service.get_health()
        if health.get("storage") != "postgres" or health.get("writable") is not True:
            raise RuntimeError("Lead database is not writable")
        print("SQL backend startup: OK", flush=True)
        print("PostgreSQL write prerequisites: OK", flush=True)
        print(f"Registered clinics: {len(app.state.knowledge_base_resolver.company_ids())}", flush=True)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        with TemporaryDirectory(prefix="lead-startup-") as temp:
            original_snapshot = os.environ.get("SESSION_SNAPSHOT_FILE")
            snapshot = Path(temp) / "sessions.json"
            if original_snapshot and Path(original_snapshot).is_file():
                shutil.copyfile(original_snapshot, snapshot)
            # Exercise startup using temporary state and leave all production workers idle.
            os.environ.update(
                LOGS_DIR=temp,
                SESSION_SNAPSHOT_FILE=str(snapshot) if snapshot.exists() else "",
                CONVERSATIONS_ARCHIVE_ENABLED="false",
                SESSION_EVICTION_ENABLED="false",
                LEADS_ARCHIVE_ENABLED="false",
                ANALYTICS_PRUNE_ENABLED="false",
                TELEGRAM_BRIDGE_ENABLED="false",
                TELEGRAM_BOT_TOKEN="",
                WATCHDOG_ENABLED="false",
                OPS_COMMANDS_ENABLED="false",
                CLIENT_LOGIN_ENABLED="false",
                LLM_PROVIDER="mock",
                LOG_LEVEL="ERROR",
            )
            for key in ("LEADS_FILE", "LEADS_ARCHIVE_FILE", "CONVERSATIONS_ARCHIVE_FILE",
                        "ANALYTICS_FILE", "ANALYTICS_ROLLUP_FILE", "DELIVERY_OUTBOX_FILE",
                        "TELEGRAM_BRIDGE_FAILURES_FILE", "TELEGRAM_PENDING_CARDS_FILE",
                        "WATCHDOG_STATE_FILE", "SYSTEM_EVENTS_FILE"):
                os.environ[key] = str(Path(temp) / key.lower())
            asyncio.run(check_startup())
        print("SQL backend shutdown: OK")
        return 0
    except Exception as error:
        print(f"SQL startup check failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
