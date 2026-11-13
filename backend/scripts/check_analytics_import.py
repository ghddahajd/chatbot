"""Import analytics from a frozen JSONL copy and optionally compare the funnel."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from hashlib import file_digest
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.storage.analytics_import import insert_events, snapshot_rows


async def run(args):
    import sqlalchemy as sa

    from app.analytics import AnalyticsService
    from app.db.connection import make_engine
    from app.db.models import Tenant
    from app.storage.postgres_funnel import funnel_events, project_metrics

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
            rows = list(snapshot_rows(handle, company=args.company))
            if not rows:
                raise ValueError("no_company_events")
            inserted = await insert_events(connection, rows) if args.apply else 0
            report = {"selected": len(rows), "inserted": inserted, "mode": "apply" if args.apply else "dry-run"}
            if args.apply and args.start is not None:
                sql_start = perf_counter()
                actual = project_metrics(*await funnel_events(
                    connection, company_id=args.company,
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
