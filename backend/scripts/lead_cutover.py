"""Inspect file queues, freeze a stopped backend's lead files, or verify the SQL import."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from import_lead_history import matches_history, scan_sources

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = "chat-widget-backend"
FILES = {
    "LEADS_FILE": "leads.jsonl",
    "LEADS_ARCHIVE_FILE": "leads_archive.jsonl",
    "DELIVERY_OUTBOX_FILE": "delivery_outbox.jsonl",
    "TELEGRAM_PENDING_CARDS_FILE": "telegram_pending_cards.jsonl",
}
LEADS, ARCHIVE, OUTBOX, TELEGRAM = FILES.values()
BLOCKING = {"pending", "failed", "processing", "unknown"}


class CutoverError(RuntimeError):
    pass


def inspect_backend() -> tuple[str, str, str]:
    keys = (*FILES, "LEAD_STORAGE_BACKEND")
    condition = " ".join(f'(eq (index (split . "=") 0) "{key}")' for key in keys)
    template = (
        "{{.State.Status}}\n{{.State.StartedAt}}\n{{json .Mounts}}\n"
        "{{range .Config.Env}}{{if or " + condition + "}}{{println .}}{{end}}{{end}}"
    )
    result = subprocess.run(
        ["docker", "inspect", "--format", template, CONTAINER], capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        raise CutoverError("backend_inspect_failed")
    state, started, mounts, *values = result.stdout.splitlines()
    env = dict(line.split("=", 1) for line in values if "=" in line)
    mount = next((mount for mount in json.loads(mounts) if mount["Destination"] == "/app/logs"), None)
    if (mount is None or mount["Type"] != "bind"
            or Path(mount["Source"]).resolve() != (ROOT / "backend/logs").resolve()):
        raise CutoverError("unexpected_backend_logs_mount")
    if any(env.get(key, f"/app/logs/{name}") != f"/app/logs/{name}" for key, name in FILES.items()):
        raise CutoverError("unexpected_backend_log_paths")
    storage = env.get("LEAD_STORAGE_BACKEND", "files")
    if storage not in {"files", "postgres"}:
        raise CutoverError("unexpected_storage_mode")
    return state, started, storage


def read_rows(path: Path, *, optional: bool = False):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CutoverError(f"unexpected_log_file:{path.name}")
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if optional:
            return
        raise
    with handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError
            except (ValueError, RecursionError):
                raise CutoverError(f"invalid_jsonl:{path.name}:{line_no}") from None
            yield row


def file_report(logs: Path) -> dict:
    companies = Counter()
    for name in (LEADS, ARCHIVE):
        for row in read_rows(logs / name, optional=name == ARCHIVE):
            company = row.get("company_id")
            label = company if isinstance(company, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", company) else "unknown"
            companies[label] += 1
    latest = {}
    for row in read_rows(logs / OUTBOX, optional=True):
        delivery_id = row.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise CutoverError("outbox_delivery_id_missing")
        status = row.get("status")
        latest[delivery_id] = status if isinstance(status, str) and status in BLOCKING | {"sent", "dead"} else "unknown"
    counts = Counter(latest.values())
    return {
        "file_leads_by_company": dict(companies),
        "file_outbox": dict(counts),
        "file_outbox_waiting": sum(counts[key] for key in BLOCKING),
        "telegram_cards_waiting": sum(1 for _ in read_rows(logs / TELEGRAM, optional=True)),
    }


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def freeze_files(parent: Path) -> dict:
    before = inspect_backend()
    if before[0] != "exited" or before[2] != "files":
        raise CutoverError("stop_file_backend_before_snapshot")
    logs = ROOT / "backend/logs"
    report = file_report(logs)
    if report["file_outbox_waiting"] or report["telegram_cards_waiting"]:
        raise CutoverError("file_deliveries_still_waiting")

    folder = Path(tempfile.mkdtemp(prefix="chatbot-cutover.", dir=parent))
    hashes = {}
    for name in (LEADS, ARCHIVE):
        source = logs / name
        if name == ARCHIVE and not source.exists():
            continue
        target = folder / name
        shutil.copyfile(source, target)
        hashes[name] = digest(target)
        os.chmod(target, 0o400)
    if inspect_backend() != before:
        raise CutoverError("backend_changed_during_snapshot")
    # db-tools runs as UID 1000. The snapshot lives under the host's private /root.
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps(hashes, indent=2) + "\n")
    for path in (*[folder / name for name in hashes], manifest, folder):
        os.chown(path, 1000, 1000)
    os.chmod(manifest, 0o400)
    return {**report, "snapshot": str(folder), "archive_included": ARCHIVE in hashes}


async def verify_import(folder: Path) -> dict:
    import sqlalchemy as sa

    from app.db.connection import make_engine
    from app.db.lead_models import delivery_outbox, leads
    from app.db.models import Tenant
    from app.db.request_models import chat_requests

    hashes = json.loads((folder / "manifest.json").read_text())
    if not isinstance(hashes, dict) or LEADS not in hashes or set(hashes) - {LEADS, ARCHIVE}:
        raise CutoverError("invalid_snapshot_manifest")
    if any(digest(folder / name) != checksum for name, checksum in hashes.items()):
        raise CutoverError("snapshot_changed")
    sources = [("leads", folder / LEADS)]
    if ARCHIVE in hashes:
        sources.append(("archive", folder / ARCHIVE))
    engine = make_engine("LEAD_")
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            tenants = dict((await connection.execute(sa.select(Tenant.company_id, Tenant.id))).all())
            stored = {row["lead_id"]: row for row in (await connection.execute(sa.select(leads))).mappings()}
            queue = dict((await connection.execute(
                sa.select(delivery_outbox.c.status, sa.func.count()).group_by(delivery_outbox.c.status)
            )).all())
            receipts = await connection.scalar(sa.select(sa.func.count()).select_from(chat_requests))
    finally:
        await engine.dispose()
    expected, report = scan_sources(sources, tenants)
    by_id = {tenant: company for company, tenant in tenants.items()}
    report.update(
        sql_by_company=dict(Counter(by_id.get(row["tenant_id"], "unknown") for row in stored.values())),
        missing=len(expected.keys() - stored.keys()),
        unexpected=len(stored.keys() - expected.keys()),
        changed=sum(not matches_history(stored[key], expected[key]) for key in expected.keys() & stored.keys()),
        sql_outbox=queue,
        sql_request_receipts=receipts,
    )
    report["verified"] = not any(report[key] for key in (
        "errors", "duplicates", "missing", "unexpected", "changed", "sql_outbox", "sql_request_receipts",
    ))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("snapshot").add_argument("--parent", type=Path, default=Path("/root"))
    sub.add_parser("verify").add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "status":
            state, _, storage = inspect_backend()
            report = {"backend": state, "storage": storage, **file_report(ROOT / "backend/logs")}
        elif args.command == "snapshot":
            report = freeze_files(args.parent)
        else:
            report = asyncio.run(verify_import(args.snapshot))
    except CutoverError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("verified", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
