"""Import analytics from a frozen JSONL copy and optionally compare the funnel."""

import argparse
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from hashlib import file_digest, sha256
from itertools import batched
import json
from pathlib import Path
import sys
from time import perf_counter
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def snapshot_rows(handle, tenant_id, company_id):
    occurrences = Counter()
    for number, raw in enumerate(handle, 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError
            if row.get("company_id") != company_id:
                continue
            timestamp = datetime.fromisoformat(row["timestamp"])
            event_type = row["event_type"]
            if timestamp.tzinfo is not None or not isinstance(event_type, str) or not event_type:
                raise ValueError
            metadata = row.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            session_id = row.get("session_id") or None
            page = metadata.get("page") or None
            if (session_id is not None and not isinstance(session_id, str)
                    or page is not None and not isinstance(page, str)):
                raise ValueError
            if "event_id" in row:
                event_key = f"id:{UUID(row['event_id'])}"
            else:
                # Legacy events can be identical. Keep their multiplicity across rewrites.
                canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                fingerprint = sha256(canonical.encode("ascii")).hexdigest()
                occurrences[fingerprint] += 1
                event_key = f"legacy:{fingerprint}:{occurrences[fingerprint]}"
            yield dict(
                tenant_id=tenant_id, event_key=event_key,
                occurred_at=timestamp.replace(tzinfo=timezone.utc), event_type=event_type,
                session_id=session_id, page=page, visitor_id=str(metadata.get("visitor_id") or "") or None,
            )
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
            raise ValueError(f"invalid_event_at_line:{number}") from None


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


async def run(args):
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert

    from app.analytics import AnalyticsService
    from app.db.analytics_models import analytics_events
    from app.db.connection import make_engine
    from app.db.models import Tenant
    from app.storage.postgres_funnel import funnel_events

    engine = make_engine("ANALYTICS_")
    try:
        async with AsyncExitStack() as stack:
            handle = stack.enter_context(args.events.open("rb"))
            connection = await stack.enter_async_context(engine.begin())
            digest = file_digest(handle, "sha256").hexdigest()
            handle.seek(0)
            tenant_id = await connection.scalar(sa.select(Tenant.id).where(Tenant.company_id == args.company))
            if tenant_id is None:
                raise ValueError("unknown_company")
            selected = inserted = 0
            for batch in batched(snapshot_rows(handle, tenant_id, args.company), 1000):
                selected += len(batch)
                if args.apply:
                    stmt = insert(analytics_events).on_conflict_do_nothing().returning(analytics_events.c.event_key)
                    inserted += len((await connection.execute(stmt, list(batch))).all())
            if not selected:
                raise ValueError("no_company_events")
            report = {"selected": selected, "inserted": inserted, "mode": "apply" if args.apply else "dry-run"}
            if args.apply and args.start is not None:
                sql_start = perf_counter()
                actual = project_metrics(*await funnel_events(
                    connection, tenant_id=tenant_id,
                    start=args.start.replace(tzinfo=timezone.utc), end=args.end.replace(tzinfo=timezone.utc),
                ))
                report["sql_ms"] = round((perf_counter() - sql_start) * 1000, 2)
                file_start = perf_counter()
                reference = AnalyticsService(analytics_file=args.events, leads_file=args.leads).conversion_funnel(
                    args.company, start=args.start, end=args.end,
                )
                report["jsonl_ms"] = round((perf_counter() - file_start) * 1000, 2)
                stages = reference.pop("stages")
                expected = {key: reference[key] for key in actual if key in reference}
                expected.update(stages=[stage["count"] for stage in stages[:3]],
                                page_loads=stages[0]["page_loads"], dialog_percent=stages[2]["percent_of_previous"])
                report["differences"] = [key for key in actual if actual[key] != expected[key]]
                if report["differences"]:
                    print(json.dumps(report), file=sys.stderr)
                    raise ValueError("funnel_mismatch")
            handle.seek(0)
            if file_digest(handle, "sha256").hexdigest() != digest:
                raise ValueError("snapshot_changed")
        return report
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--leads", type=Path)
    parser.add_argument("--company", required=True)
    parser.add_argument("--start", type=datetime.fromisoformat)
    parser.add_argument("--end", type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if any(value is not None for value in (args.start, args.end, args.leads)):
        if args.start is None or args.end is None or args.leads is None:
            parser.error("Funnel comparison needs --start, --end and --leads")
        if args.start.tzinfo is not None or args.end.tzinfo is not None or args.start > args.end:
            parser.error("Provide ordered UTC timestamps without an offset, as stored in JSONL")
    try:
        print(json.dumps(asyncio.run(run(args))))
    except ValueError as error:
        message = str(error)
        known = {"unknown_company", "no_company_events", "funnel_mismatch", "snapshot_changed"}
        print(message if message in known or message.startswith("invalid_event_at_line:") else "ValueError", file=sys.stderr)
        return 1
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
