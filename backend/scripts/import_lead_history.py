"""Check, import or prepare rollback JSONL without starting the application."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SQL_ID = "_sql_lead_id"


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
    if SQL_ID in payload:
        sql_id = payload.pop(SQL_ID)
        try:
            if not isinstance(sql_id, str) or str(UUID(sql_id)) != sql_id:
                raise ValueError
        except ValueError:
            raise ValueError("invalid_sql_lead_id") from None
        lead_id = sql_id
    else:
        lead_id = f"jsonl:{digest}"
        # Legacy panel links may contain credentials.
        payload.pop("operator_url", None)
    trigger = payload.get("lead_trigger")
    booking = trigger == "booking_request" or (not trigger and payload.get("reason") == "booking")
    return {
        "lead_id": lead_id, "tenant_id": tenants[company_id],
        "session_id": session_id, "created_at": created_at, "payload": payload,
        "event_type": "booking_created" if booking else "lead_created",
    }


def scan_sources(sources, tenants: dict, active_lines=None) -> tuple[dict, dict]:
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
                previous = rows.get(row["lead_id"])
                if previous is not None:
                    if previous != row:
                        errors.append({**location, "reason": "conflicting_lead_id"})
                    else:
                        duplicates.append(location)
                    continue
                rows[row["lead_id"]] = row
                counts[row["payload"]["company_id"]] += 1
                if source == "leads" and active_lines is not None:
                    active_lines.append(raw.rstrip(b"\r\n") + b"\n")
    return rows, {"by_company": dict(counts), "unique_rows": len(rows), "duplicates": duplicates, "errors": errors}


def matches_history(existing, row) -> bool:
    # The legacy file has no explicit event_type for an already-saved SQL lead.
    legacy = row["lead_id"].startswith("jsonl:")
    return existing is not None and all(
        existing[key] == value for key, value in row.items() if legacy or key != "event_type"
    )


async def import_history(engine, sources, *, apply: bool = False) -> dict:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import insert

    from app.db.lead_models import leads
    from app.db.models import Tenant

    async with engine.connect() as connection:
        tenants = dict((await connection.execute(sa.select(Tenant.company_id, Tenant.id))).all())
    rows, report = scan_sources(sources, tenants)
    report.update(mode="apply" if apply else "dry-run", inserted=0, already_present=0)
    if report["errors"] or not rows:
        return report
    inserted = 0
    async with engine.begin() as connection:
        sql_rows = [row for lead_id, row in rows.items() if not lead_id.startswith("jsonl:")]
        for row in sql_rows:
            existing = (await connection.execute(
                sa.select(leads).where(leads.c.lead_id == row["lead_id"], leads.c.tenant_id == row["tenant_id"])
            )).mappings().one_or_none()
            if not matches_history(existing, row):
                report["errors"].append({"reason": "sql_lead_missing_or_changed"})
                return report
        if not apply:
            return report
        # Import only the history table. Delivery workers have nothing to replay.
        for lead_id, row in rows.items():
            if not lead_id.startswith("jsonl:"):
                continue
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
            if not matches_history(existing, row):
                raise RuntimeError("history_conflict")
    report.update(inserted=inserted, already_present=len(rows) - inserted)
    return report


def write_new_file(path: Path, lines: list[bytes]) -> None:
    # A stopped export must not leave a partial result at the requested path.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".lead-rollback-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink()


async def prepare_rollback(engine, sources, output: Path) -> dict:
    import sqlalchemy as sa

    from app.db.lead_models import delivery_outbox, leads
    from app.db.models import Tenant

    async with engine.begin() as connection:
        await connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        tenants = dict((await connection.execute(sa.select(Tenant.company_id, Tenant.id))).all())
        stored = (await connection.execute(sa.select(leads).order_by(leads.c.created_at, leads.c.lead_id))).mappings().all()
        queue = dict((await connection.execute(
            sa.select(delivery_outbox.c.status, sa.func.count()).group_by(delivery_outbox.c.status)
        )).all())
    lines = []
    original, report = scan_sources(sources, tenants, active_lines=lines)
    report.update(mode="rollback", exported=0, new_by_company={}, sql_outbox=queue,
                  sql_outbox_waiting=sum(queue.get(status, 0) for status in ("pending", "failed", "processing")))
    if report["errors"] or report["duplicates"]:
        return report
    existing = {row["lead_id"]: row for row in stored}
    if any(not matches_history(existing.get(lead_id), row) for lead_id, row in original.items()):
        report["errors"].append({"reason": "baseline_missing_or_changed"})
        return report
    new_by_company = Counter()
    for row in stored:
        lead_id = row["lead_id"]
        if lead_id in original:
            continue
        if lead_id.startswith("jsonl:"):
            report["errors"].append({"reason": "baseline_history_missing"})
            return report
        payload = {**row["payload"], SQL_ID: lead_id}
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if not matches_history(row, parse_row(raw, tenants)):
                raise ValueError
        except (ValueError, RecursionError):
            report["errors"].append({"reason": "invalid_saved_sql_lead"})
            return report
        lines.append(raw + b"\n")
        new_by_company[payload["company_id"]] += 1
    write_new_file(output, lines)
    report.update(exported=sum(new_by_company.values()), new_by_company=dict(new_by_company))
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
        if args.rollback_output is not None:
            return await prepare_rollback(engine, sources, args.rollback_output)
        return await import_history(engine, sources, apply=args.apply)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leads", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write validated history; default is dry-run")
    mode.add_argument("--rollback-output", type=Path, help="prepare a new active JSONL using frozen original files and SQL leads")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] or (report["mode"] == "rollback" and report["duplicates"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
