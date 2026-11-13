"""Synchronize and read the historical part of activity graphs."""

import asyncio
from itertools import batched
from uuid import uuid4

import sqlalchemy as sa

from ..analytics import analytics_file_version
from ..db.analytics_models import analytics_rollup
from ..utils.jsonl import read_jsonl


def rollup_version(path):
    try:
        return analytics_file_version(path)
    except FileNotFoundError:
        return None


async def sync_rollup(connection, path, previous):
    version = rollup_version(path)
    if previous is not None and version == previous[0]:
        return previous
    rows = await asyncio.to_thread(read_jsonl, path) if version is not None else []
    if rollup_version(path) != version:
        return None

    generation = uuid4()
    await connection.execute(analytics_rollup.delete())
    values = (
        dict(position=position, generation=generation, company_id=row.get("company_id"),
             day_key=str(row.get("date") or ""), payload=row)
        for position, row in enumerate(rows)
        if row.get("event_type", "message_answered") == "message_answered"
    )
    for batch in batched(values, 1000):
        await connection.execute(analytics_rollup.insert(), list(batch))
    return version, generation


async def load_rollup(connection, *, company_id, start, end, generation):
    c = analytics_rollup.c
    scope = [c.generation == generation, c.day_key >= start.date().isoformat()]
    if company_id is not None:
        scope.append(c.company_id == company_id)
    if end is not None:
        scope.append(c.day_key <= end.date().isoformat())
    rows = await connection.scalars(sa.select(c.payload).where(*scope))
    return rows.all()
