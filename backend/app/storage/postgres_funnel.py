"""Read event metrics for one clinic and period."""

import sqlalchemy as sa

from ..db.analytics_models import analytics_events


async def funnel_events(connection, *, tenant_id, start, end):
    columns = analytics_events.c
    scope = (
        columns.tenant_id == tenant_id,
        columns.occurred_at.between(start, end),
    )
    counts = await connection.execute(
        sa.select(
            columns.event_type,
            sa.func.count().label("rows"),
            sa.func.count().filter(columns.visitor_id.is_(None)).label("without_visitor"),
            sa.func.count(sa.distinct(columns.visitor_id)).label("visitors"),
            sa.func.count(sa.distinct(columns.session_id)).label("sessions"),
        ).where(*scope).group_by(columns.event_type)
    )
    metrics = {row.event_type: dict(row._mapping) for row in counts}
    impression = columns.event_type == "widget_impression"
    opened = columns.event_type == "chat_opened"
    answered = columns.event_type == "message_answered"
    loads = sa.func.count().filter(impression).label("loads")
    opens = sa.func.count().filter(opened).label("opens")
    dialogs = sa.func.count(sa.distinct(columns.session_id)).filter(answered).label("dialogs")
    pages = await connection.execute(
        sa.select(columns.page, loads, opens, dialogs)
        .where(*scope, columns.page.is_not(None), sa.or_(impression, opened, answered & columns.session_id.is_not(None)))
        .group_by(columns.page)
        .order_by(loads.desc(), opens.desc(), columns.page.collate("C"))
        .limit(20)
    )
    return metrics, [dict(row) for row in pages.mappings()]
