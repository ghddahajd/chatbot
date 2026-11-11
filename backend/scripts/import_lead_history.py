"""Check or import historical lead JSONL without starting the application."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_row(raw: bytes, tenants: dict) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ValueError("invalid_json") from None
    if not isinstance(payload, dict):
        raise ValueError("not_an_object")
    company_id = payload.get("company_id")
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("missing_company")
    if company_id not in tenants:
        raise ValueError("unknown_company")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("missing_session")
    try:
        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError
        created_at = datetime.fromisoformat(timestamp)
        created_at = (created_at.replace(tzinfo=timezone.utc) if created_at.tzinfo is None
                      else created_at.astimezone(timezone.utc))
    except (ValueError, OverflowError):
        raise ValueError("invalid_timestamp") from None
    if payload.get("needs_operator") is not None and not isinstance(payload["needs_operator"], bool):
        raise ValueError("invalid_needs_operator")
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if r"\u0000" in canonical.replace("\\\\", ""):
            raise ValueError
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (ValueError, RecursionError):
        raise ValueError("invalid_json_value") from None
    # Legacy panel links may contain credentials.
    payload.pop("operator_url", None)
    trigger = payload.get("lead_trigger")
    booking = trigger == "booking_request" or (not trigger and payload.get("reason") == "booking")
    return {
        "lead_id": f"jsonl:{digest}", "tenant_id": tenants[company_id],
        "session_id": session_id, "created_at": created_at, "payload": payload,
        "event_type": "booking_created" if booking else "lead_created",
    }


def scan_sources(sources, tenants: dict) -> tuple[dict, dict]:
    rows, errors, duplicates = {}, [], []
    counts = Counter()
    for source, path in sources:
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                location = {"source": source, "line": number}
                try:
                    row = parse_row(raw, tenants)
                except ValueError as error:
                    errors.append({**location, "reason": str(error)})
                    continue
                if row["lead_id"] in rows:
                    duplicates.append(location)
                    continue
                rows[row["lead_id"]] = row
                counts[row["payload"]["company_id"]] += 1
    return rows, {"by_company": dict(counts), "unique_rows": len(rows), "duplicates": duplicates, "errors": errors}


async def import_history(engine, sources, *, apply: bool = False) -> dict:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert

    from app.db.lead_models import leads
    from app.db.models import Tenant

    async with engine.connect() as connection:
        tenants = dict((await connection.execute(sa.select(Tenant.company_id, Tenant.id))).all())
    rows, report = scan_sources(sources, tenants)
    report.update(mode="apply" if apply else "dry-run", inserted=0, already_present=0)
    if not apply or report["errors"] or not rows:
        return report
    inserted = 0
    async with engine.begin() as connection:
        # Import only the history table. Delivery workers have nothing to replay.
        for row in rows.values():
            result = await connection.scalar(
                insert(leads).values(**row).on_conflict_do_nothing(index_elements=[leads.c.lead_id])
                .returning(leads.c.lead_id)
            )
            if result is not None:
                inserted += 1
                continue
            existing = (await connection.execute(
                sa.select(leads).where(leads.c.lead_id == row["lead_id"], leads.c.tenant_id == row["tenant_id"])
            )).mappings().one_or_none()
            if existing is None or any(existing[key] != value for key, value in row.items()):
                raise RuntimeError("history_conflict")
    report.update(inserted=inserted, already_present=len(rows) - inserted)
    return report


async def run(args) -> dict:
    from app.db.connection import make_engine

    sources = [("leads", args.leads)]
    if args.archive is not None:
        if args.archive.resolve() == args.leads.resolve():
            raise ValueError("same_source")
        sources.append(("archive", args.archive))
    engine = make_engine("LEAD_")
    try:
        return await import_history(engine, sources, apply=args.apply)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leads", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--apply", action="store_true", help="write validated history; default is dry-run")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
