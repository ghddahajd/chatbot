"""Read event metrics for one clinic and period."""

from datetime import timezone

import sqlalchemy as sa

from ..analytics import analytics_snapshot_matches
from ..db.analytics_models import analytics_events


async def funnel_events(connection, *, company_id, start, end, generation=None):
    columns = analytics_events.c
    scope = [
        columns.occurred_at >= start,
    ]
    if company_id is not None:
        scope.append(columns.company_id == company_id)
    if end is not None:
        scope.append(columns.occurred_at <= end)
    if generation is not None:
        scope.append(columns.file_generation == generation)
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


def project_metrics(counts, pages):
    def count(event_type, key):
        return counts.get(event_type, {}).get(key, 0)

    def visitors(event_type):
        return count(event_type, "visitors") + count(event_type, "without_visitor")

    opened = visitors("chat_opened")
    dialogs = count("message_answered", "sessions")
    for page in pages:
        page["open_rate"] = round(page["opens"] / page["loads"] * 100, 1) if page["loads"] else None
    return {
        "stages": [visitors("widget_impression"), opened, dialogs],
        "page_loads": count("widget_impression", "rows"),
        "dialog_percent": round(dialogs / opened * 100, 1) if opened and not count("chat_opened", "without_visitor") else None,
        "contact_clicks": {"call": visitors("contact_call_clicked"), "telegram": visitors("contact_telegram_clicked")},
        "teaser": {"shown": visitors("teaser_shown"), "price": visitors("teaser_price_clicked"),
                   "booking": visitors("teaser_booking_clicked")},
        "booking_card": {key: visitors(f"booking_card_{suffix}") for key, suffix in
                         (("shown", "shown"), ("day", "day_picked"), ("phone", "phone_started"), ("sent", "sent"))},
        "idle_reminder": {key: visitors(f"idle_reminder_{suffix}") for key, suffix in
                          (("shown", "shown"), ("callback", "callback_clicked"), ("later", "later_clicked"))},
        "pages": pages,
    }


async def load_funnel_events(engine, *, company_id, start, end, path, version, generation):
    if not analytics_snapshot_matches(path, version):
        return None
    async with engine.connect() as connection:
        counts, pages = await funnel_events(
            connection, company_id=company_id, start=start.replace(tzinfo=timezone.utc),
            end=end.replace(tzinfo=timezone.utc) if end is not None else None,
            generation=generation,
        )
    if not analytics_snapshot_matches(path, version):
        return None
    return project_metrics(counts, pages)
