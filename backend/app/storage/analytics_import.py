"""Stable event identities shared by the offline importer and the sync worker."""

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from itertools import batched
import json
from uuid import UUID


def snapshot_rows(handle, tenants, *, skipped_companies: set[str | None] | None = None):
    occurrences = Counter()
    for number, raw in enumerate(handle, 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError
            company_id = row.get("company_id")
            tenant_id = tenants.get(company_id)
            if tenant_id is None:
                if skipped_companies is not None:
                    skipped_companies.add(company_id)
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
            question = None
            if event_type in {"message_answered", "unknown_question"}:
                question = str(row.get("message") or "").strip() or None
            category_field = {"message_answered": "policy_reason", "objection_raised": "objection_topic"}.get(event_type)
            category = str(metadata.get(category_field) or "unknown") if category_field else None
            claimed_by = str(metadata.get("claimed_by") or "unknown") if event_type == "operator_claimed" else None
            unanswered = None
            if event_type == "unknown_question":
                unanswered = {key: row.get(key) for key in ("timestamp", "company_id", "session_id", "message")}
                unanswered.update(reason=metadata.get("reason"), service_id=metadata.get("service_id"))
            yield dict(
                tenant_id=tenant_id, event_key=event_key,
                occurred_at=timestamp.replace(tzinfo=timezone.utc), event_type=event_type,
                session_id=session_id, page=page, visitor_id=str(metadata.get("visitor_id") or "") or None,
                file_position=number, question=question, question_key=question.lower() if question else None,
                category=category, claimed_by=claimed_by, unanswered=unanswered,
            )
        except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
            raise ValueError(f"invalid_event_at_line:{number}") from None


async def insert_events(connection, rows, *, generation=None):
    from sqlalchemy.dialects.postgresql import insert
    from ..db.analytics_models import analytics_events

    statement = insert(analytics_events)
    if generation is None:
        statement = statement.on_conflict_do_nothing()
    else:
        statement = statement.on_conflict_do_update(
            index_elements=list(analytics_events.primary_key),
            set_={name: statement.excluded[name] for name in (
                "file_generation", "file_position", "question", "question_key", "category", "claimed_by", "unanswered",
            )},
            where=analytics_events.c.file_generation.is_distinct_from(generation),
        )
    statement = statement.returning(analytics_events.c.event_key)
    inserted = 0
    for batch in batched(rows, 1000):
        values = list(batch)
        if generation is not None:
            values = list({
                (row["tenant_id"], row["event_key"]): {**row, "file_generation": generation}
                for row in reversed(batch)
            }.values())
        inserted += len((await connection.execute(statement, values)).all())
    return inserted
