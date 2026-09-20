"""вытаскивает переписки по префиксам session_id в один компактный текстовый файл.

Читает оба источника: conversations_archive.jsonl (закрытые/эвиктнутые сессии) и
session_snapshot.json (ещё живые в памяти) — свежий диалог в архив попадает только после
эвикции, поэтому одного архива мало.

    python3 backend/scripts/export_dialogs.py 6ddfecd5 50d9b119
    python3 backend/scripts/export_dialogs.py --last 5
    python3 backend/scripts/export_dialogs.py --since-hours 24 --company rosh_import_demo

Время в файле — UTC (как хранится), в шапке диалога добавлено +3 (МСК) рядом.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROLE_TAG = {"user": "U", "assistant": "A", "operator": "O", "system": "S"}


def load_sessions(archive: Path, snapshot: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def keep(record: dict) -> None:
        sid = record.get("session_id")
        if not sid:
            return
        prev = found.get(sid)
        # один и тот же диалог мог попасть и в снапшот, и в архив — оставляем более полный
        if prev is None or len(record.get("messages", [])) >= len(prev.get("messages", [])):
            found[sid] = record

    if archive.exists():
        for line in archive.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    keep(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if snapshot.exists():
        try:
            data = json.loads(snapshot.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = []
        for record in data if isinstance(data, list) else []:
            keep(record)
    return found


def started_at(record: dict) -> datetime:
    return datetime.fromisoformat(record.get("created_at") or "1970-01-01T00:00:00")


def render(record: dict) -> str:
    start = started_at(record)
    msk = start + timedelta(hours=3)
    head = (
        f"== {record['session_id'][:8]} | {record.get('company_id')} | {record.get('status')} | "
        f"{start:%Y-%m-%d %H:%M} UTC ({msk:%H:%M} МСК)"
    )
    flags = [name for name in ("lead_requested", "operator_requested") if record.get(name)]
    if flags:
        head += " | " + ",".join(flags)
    lines = [head]
    for message in record.get("messages", []):
        stamp = (message.get("created_at") or "")[11:16]
        tag = ROLE_TAG.get(message.get("role"), "?")
        text = " ".join(str(message.get("text") or "").split())
        lines.append(f"[{stamp}] {tag}: {text}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prefixes", nargs="*", help="начало session_id (обычно 8 символов)")
    parser.add_argument("--last", type=int, help="последние N диалогов вместо списка id")
    parser.add_argument("--since-hours", type=float, help="все диалоги за последние N часов")
    parser.add_argument("--company", help="фильтр по company_id для --last/--since-hours")
    parser.add_argument("--out", default="/tmp/dialogs.txt")
    parser.add_argument("--archive", type=Path, default=BACKEND_DIR / "logs" / "conversations_archive.jsonl")
    parser.add_argument("--snapshot", type=Path, default=BACKEND_DIR / "data" / "session_snapshot.json")
    args = parser.parse_args()

    if not (args.prefixes or args.last or args.since_hours):
        parser.error("нужны id, либо --last N, либо --since-hours N")

    sessions = load_sessions(args.archive, args.snapshot)
    picked: list[dict] = []
    missing: list[str] = []

    for prefix in args.prefixes:
        matches = [r for sid, r in sessions.items() if sid.startswith(prefix)]
        if not matches:
            missing.append(prefix)
        picked.extend(matches)

    if args.last or args.since_hours:
        pool = [r for r in sessions.values() if not args.company or r.get("company_id") == args.company]
        if args.since_hours:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=args.since_hours)
            pool = [r for r in pool if started_at(r) >= cutoff]
        pool.sort(key=started_at)
        picked.extend(pool[-args.last:] if args.last else pool)

    seen: set[str] = set()
    unique = [r for r in picked if not (r["session_id"] in seen or seen.add(r["session_id"]))]
    unique.sort(key=started_at)

    Path(args.out).write_text("\n\n".join(render(r) for r in unique) + "\n", encoding="utf-8")
    print(f"диалогов: {len(unique)} -> {args.out}")
    if missing:
        print("не найдено (нет ни в архиве, ни в снапшоте): " + ", ".join(missing))


if __name__ == "__main__":
    main()
