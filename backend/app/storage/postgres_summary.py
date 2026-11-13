"""Dashboard summary and trends from one synchronized file generation."""

from collections import Counter
from datetime import timedelta, timezone

import sqlalchemy as sa

from ..analytics import FUNNEL_WINDOW_DAYS
from ..db.analytics_models import analytics_events
from ..db.models import Tenant


async def load_summary_reports(engine, *, company_id, generation, limit, trend=None, comparison=None):
    columns = analytics_events.c
    scope = [columns.file_generation == generation]
    if company_id is not None:
        scope.append(Tenant.company_id == company_id)
    table = analytics_events.join(Tenant.__table__)
    async with engine.connect() as connection:
        if company_id is not None and await connection.scalar(
            sa.select(Tenant.id).where(Tenant.company_id == company_id)
        ) is None:
            return None
        counts = Counter()
        by_company = {}
        grouped = await connection.execute(
            sa.select(Tenant.company_id, columns.event_type, sa.func.count()).select_from(table)
            .where(*scope).group_by(Tenant.company_id, columns.event_type)
        )
        for event_company, kind, count in grouped:
            counts[kind] += count
            by_company.setdefault(event_company, {})[kind] = count
        unanswered = (await connection.scalars(
            sa.select(columns.unanswered).select_from(table)
            .where(*scope, columns.event_type == "unknown_question")
            .order_by(columns.file_position.desc()).limit(limit)
        )).all()
        report = {
            "summary": {
                "events": {"total": sum(counts.values()), "by_type": dict(counts), "by_company": by_company},
                "unanswered": list(unanswered),
            },
        }
        if trend is not None:
            start, end = (moment.replace(tzinfo=timezone.utc) for moment in trend)
            day = sa.cast(sa.func.timezone("UTC", columns.occurred_at), sa.Date)
            rows = await connection.execute(
                sa.select(day, sa.func.count()).select_from(table)
                .where(*scope, columns.event_type == "unknown_question",
                       columns.occurred_at.between(start, end)).group_by(day)
            )
            report["trend"] = {date.isoformat(): count for date, count in rows}
        if comparison is not None:
            now, days = comparison
            end = now.replace(tzinfo=timezone.utc)
            span = timedelta(days=min(days, FUNNEL_WINDOW_DAYS))
            boundary = end - span
            sessions = sa.func.count(sa.distinct(columns.session_id))
            row = (await connection.execute(
                sa.select(sessions.filter(columns.occurred_at >= boundary),
                          sessions.filter(columns.occurred_at < boundary)).select_from(table)
                .where(*scope, columns.event_type == "message_answered",
                       columns.occurred_at >= boundary - span, columns.occurred_at < end)
            )).one()
            report["comparison"] = tuple(row)
    return report
