"""Aggregate the live part of the file report in the clinic's timezone."""

from datetime import timezone

import sqlalchemy as sa

from ..analytics import analytics_snapshot_matches
from ..db.analytics_models import analytics_events
from ..hours import _resolve_timezone
from .postgres_rollup import load_rollup


async def load_activity(engine, *, company_id, start, end, timezone_name, path, version, generation,
                        rollup_generation):
    if not analytics_snapshot_matches(path, version):
        return None
    columns = analytics_events.c
    local = sa.func.timezone(_resolve_timezone(timezone_name).key, columns.occurred_at)
    hour = sa.extract("hour", local)
    weekday = sa.extract("isodow", local) - 1
    scope = [
        columns.file_generation == generation,
        columns.event_type == "message_answered",
        columns.occurred_at >= start.replace(tzinfo=timezone.utc),
    ]
    if company_id is not None:
        scope.append(columns.company_id == company_id)
    if end is not None:
        scope.append(columns.occurred_at <= end.replace(tzinfo=timezone.utc))
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.select(hour, weekday, sa.func.count())
            .where(*scope)
            .group_by(hour, weekday)
        )
        hours, weekdays = {}, {}
        for h, d, count in rows:
            hours[int(h)] = hours.get(int(h), 0) + count
            weekdays[int(d)] = weekdays.get(int(d), 0) + count
        historical = await load_rollup(connection, company_id=company_id, start=start, end=end,
                                       generation=rollup_generation)
    return (hours, weekdays, historical) if analytics_snapshot_matches(path, version) else None
