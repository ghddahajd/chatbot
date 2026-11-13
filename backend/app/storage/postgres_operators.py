"""Load scoped handover events for the shared operator calculations."""

from datetime import timezone

import sqlalchemy as sa

from ..analytics import analytics_snapshot_matches
from ..db.analytics_models import analytics_events


async def load_operator_events(engine, *, company_id, start, end, path, version, generation):
    if not analytics_snapshot_matches(path, version):
        return None
    columns = analytics_events.c
    scope = [columns.file_generation == generation]
    if company_id is not None:
        scope.append(columns.company_id == company_id)
    in_range = [columns.event_type.in_(("operator_claimed", "operator_requested"))]
    if start is not None:
        in_range.append(columns.occurred_at >= start.replace(tzinfo=timezone.utc))
    if end is not None:
        in_range.append(columns.occurred_at <= end.replace(tzinfo=timezone.utc))
    # Claims in the window can be closed after its end. Keep the file order:
    # the duration report uses the last recorded close when there are repeats.
    query = (
        sa.select(columns.company_id, columns.event_type, columns.session_id, columns.occurred_at, columns.claimed_by)
        .where(*scope,
               sa.or_(columns.event_type == "operator_closed", sa.and_(*in_range)))
        .order_by(columns.file_position)
    )
    async with engine.connect() as connection:
        rows = (await connection.execute(query)).all()
    if not analytics_snapshot_matches(path, version):
        return None
    return [
        {"company_id": event_company, "event_type": kind, "session_id": session_id,
         "timestamp": occurred_at.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
         "metadata": {"claimed_by": claimed_by}}
        for event_company, kind, session_id, occurred_at, claimed_by in rows
    ]
