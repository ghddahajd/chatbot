"""Aggregate the live part of the file report in the clinic's timezone."""

from datetime import timezone

import sqlalchemy as sa

from ..analytics import analytics_file_version
from ..db.analytics_models import analytics_events
from ..db.models import Tenant
from ..hours import _resolve_timezone


async def load_activity(engine, *, company_id, start, end, timezone_name, path, version, generation):
    if analytics_file_version(path) != version:
        return None
    columns = analytics_events.c
    local = sa.func.timezone(_resolve_timezone(timezone_name).key, columns.occurred_at)
    hour = sa.extract("hour", local)
    weekday = sa.extract("isodow", local) - 1
    scope = [
        Tenant.company_id == company_id,
        columns.file_generation == generation,
        columns.event_type == "message_answered",
        columns.occurred_at >= start.replace(tzinfo=timezone.utc),
    ]
    if end is not None:
        scope.append(columns.occurred_at <= end.replace(tzinfo=timezone.utc))
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.select(hour, weekday, sa.func.count())
            .select_from(analytics_events.join(Tenant.__table__))
            .where(*scope)
            .group_by(hour, weekday)
        )
        hours, weekdays = {}, {}
        for h, d, count in rows:
            hours[int(h)] = hours.get(int(h), 0) + count
            weekdays[int(d)] = weekdays.get(int(d), 0) + count
    return (hours, weekdays) if analytics_file_version(path) == version else None
