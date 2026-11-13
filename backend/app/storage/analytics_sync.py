"""Mirror JSONL outside the request path and prune only a committed file version."""

import asyncio
import logging
from time import monotonic
from uuid import uuid4

from ..analytics import analytics_file_version, archive_old_analytics_events
from .analytics_import import insert_events, snapshot_rows


logger = logging.getLogger(__name__)


def read_snapshot(path, tenants):
    try:
        version = analytics_file_version(path)
    except FileNotFoundError:
        return None, []
    with path.open("rb") as handle:
        rows = list(snapshot_rows(handle, tenants))
    if analytics_file_version(path) != version:
        return None, []
    return version, rows


async def sync_once(engine, settings, company_ids, *, prune, previous=None):
    import sqlalchemy as sa
    from ..db.models import Tenant

    async with engine.begin() as connection:
        registry = await connection.execute(sa.select(Tenant.company_id, Tenant.id))
        tenants = {company_id: tenant_id for company_id, tenant_id in registry}
        if set(company_ids) - tenants.keys():
            raise ValueError("analytics_company_not_registered")
        version, rows = await asyncio.to_thread(read_snapshot, settings.analytics_file, tenants)
        generation = None
        if version is not None:
            generation = previous[1] if previous is not None and version == previous[0] else uuid4()
            await insert_events(connection, rows, generation=generation)
    if prune and version is not None:
        removed = await asyncio.to_thread(
            archive_old_analytics_events, settings.analytics_file, settings.analytics_rollup_file,
            settings.analytics_message_retention_days, synced_version=version, synced_companies=set(tenants),
        )
        prune_completed = bool(removed) or analytics_file_version(settings.analytics_file) == version
    else:
        prune_completed = False
    return prune_completed, (version, generation) if generation is not None else None


async def run_analytics_sync_loop(settings, resolver, state):
    engine = None
    next_prune = monotonic()
    previous = None
    try:
        while True:
            try:
                if engine is None:
                    from ..db.connection import make_engine
                    engine = make_engine("ANALYTICS_")
                prune = settings.analytics_prune_enabled and monotonic() >= next_prune
                prune_completed, previous = await sync_once(
                    engine, settings, resolver.company_ids(), prune=prune, previous=previous,
                )
                state.analytics_funnel_source = (engine, *previous) if previous is not None else None
                if prune_completed:
                    next_prune = monotonic() + settings.analytics_prune_interval_seconds
            except Exception as error:
                previous = None
                state.analytics_funnel_source = None
                logger.warning("analytics sync failed error=%s", type(error).__name__)
            await asyncio.sleep(settings.analytics_sync_interval_seconds)
    finally:
        state.analytics_funnel_source = None
        if engine is not None:
            await engine.dispose()
