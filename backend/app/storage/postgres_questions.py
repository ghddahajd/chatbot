"""Question and category reports over the synchronized JSONL generation."""

from datetime import timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import aggregate_order_by

from ..analytics import analytics_snapshot_matches
from ..db.analytics_models import analytics_events


async def load_question_reports(engine, *, company_id, start, end, path, version, generation):
    if not analytics_snapshot_matches(path, version):
        return None
    c = analytics_events.c
    scope = [
        c.file_generation == generation,
        c.occurred_at >= start.replace(tzinfo=timezone.utc),
    ]
    if company_id is not None:
        scope.append(c.company_id == company_id)
    if end is not None:
        scope.append(c.occurred_at <= end.replace(tzinfo=timezone.utc))
    count = sa.func.count().label("count")
    first = sa.func.min(c.file_position)

    async with engine.connect() as connection:
        categories = await connection.execute(
            sa.select(c.event_type, c.category, count)
            .where(*scope, c.category.is_not(None))
            .group_by(c.event_type, c.category).order_by(count.desc(), first)
        )
        breakdown = {"intent_breakdown": [], "objection_breakdown": []}
        for kind, category, total in categories:
            if kind == "message_answered":
                breakdown["intent_breakdown"].append({"reason": category, "count": total})
            elif kind == "objection_raised":
                breakdown["objection_breakdown"].append({"topic": category, "count": total})

        example = sa.func.array_agg(aggregate_order_by(c.question, c.file_position))[1].label("message")
        for kind, report in (("message_answered", "top_answered_questions"),
                             ("unknown_question", "top_unanswered_questions")):
            rows = await connection.execute(
                sa.select(example, count)
                .where(*scope, c.event_type == kind, c.question_key.is_not(None))
                .group_by(c.question_key).order_by(count.desc(), first).limit(10)
            )
            breakdown[report] = [dict(row) for row in rows.mappings()]

    return breakdown if analytics_snapshot_matches(path, version) else None
