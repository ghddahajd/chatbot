"""Opt-in PostgreSQL persistence; no HTTP, runtime activation or schema creation."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db.models import Tenant
from ..db.lead_models import delivery_outbox as outbox, leads
from ..db.request_models import chat_requests


def _required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Required string: {name}")
    return value


def _datetime(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Invalid timestamp")
    # Legacy JSONL timestamps are naive UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class PostgresLeadOutboxStore:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Fail closed on unmigrated schema or an overprivileged connection."""
        async with self.sessions() as session, session.begin():
            role = (await session.execute(sa.text("""
                SELECT current_user AS name, rolsuper, rolcreatedb, rolcreaterole,
                       rolbypassrls, rolreplication,
                       EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid) AS member,
                       EXISTS (SELECT 1 FROM pg_database WHERE datname=current_database()
                               AND datdba=r.oid) AS db_owner
                FROM pg_roles r WHERE rolname=current_user
            """))).mappings().one()
            if role["name"] != "chatbot_lead_runtime" or any(role[k] for k in role if k != "name"):
                raise RuntimeError("Lead storage requires an isolated chatbot_lead_runtime role")
            unsafe_schema = await session.scalar(sa.text("""
                SELECT EXISTS (SELECT 1 FROM pg_namespace
                  WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
                  AND (has_schema_privilege(current_user,oid,'CREATE')
                       OR nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)))
            """))
            if unsafe_schema:
                raise RuntimeError("Lead runtime must not own or create schemas")
            permissions = (await session.execute(sa.text("""
                SELECT n.nspname, c.relname, p.privilege,
                       has_table_privilege(current_user,c.oid,p.privilege) AS allowed,
                       c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AS owner,
                       CASE WHEN p.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES')
                         THEN has_any_column_privilege(current_user,c.oid,p.privilege)
                         ELSE false END AS column_allowed
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(privilege)
                WHERE c.relkind IN ('r','p','v','m','f')
                  AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
            """))).mappings().all()
            expected = {("tenants", "SELECT"), ("leads", "SELECT"), ("leads", "INSERT"),
                        ("delivery_outbox", "SELECT"), ("delivery_outbox", "INSERT"),
                        ("delivery_outbox", "UPDATE"), ("chat_requests", "SELECT"),
                        ("chat_requests", "INSERT"), ("chat_requests", "UPDATE")}
            found = set()
            for row in permissions:
                key = (row["relname"], row["privilege"])
                wanted = row["nspname"] == "public" and key in expected
                if row["owner"] or (not wanted and (row["allowed"] or row["column_allowed"])):
                    raise RuntimeError("Lead runtime has excessive table privileges")
                if wanted and row["allowed"]:
                    found.add(key)
            if found != expected:
                raise RuntimeError("Apply lead/outbox migration and grant limited runtime privileges")
            # These reads verify every required column without granting access to alembic_version.
            await session.execute(sa.select(leads).limit(0))
            await session.execute(sa.select(outbox).limit(0))
            await session.execute(sa.select(chat_requests).limit(0))
            migrated = await session.scalar(sa.text("""
                SELECT count(*) = 2 FROM pg_constraint
                WHERE (conrelid='public.delivery_outbox'::regclass AND conname='fk_outbox_tenant_lead')
                   OR (conrelid='public.leads'::regclass AND conname='uq_leads_tenant_lead')
            """))
            if not migrated:
                raise RuntimeError("Lead/outbox migration constraints are missing")
            receipts_migrated = await session.scalar(sa.text("""
                SELECT count(*) = 8 FROM pg_constraint
                WHERE conrelid='public.chat_requests'::regclass
                  AND conname IN ('chat_requests_pkey', 'chat_requests_tenant_id_fkey',
                    'fk_chat_requests_tenant_lead', 'ck_chat_requests_fingerprint',
                    'ck_chat_requests_status', 'ck_chat_requests_response',
                    'ck_chat_requests_done', 'ck_chat_requests_lead_response')
            """))
            if not receipts_migrated:
                raise RuntimeError("Request receipt migration constraints are missing")

    async def close(self) -> None:
        await self.engine.dispose()

    async def validate_companies(self, company_ids) -> None:
        expected = set(company_ids)
        async with self.sessions() as session:
            registered = set((await session.scalars(sa.select(Tenant.company_id)
                                                    .where(Tenant.company_id.in_(expected)))).all())
        if expected != registered:
            raise ValueError("Register every active KB in the tenant registry before enabling storage")

    async def _tenant(self, session, company_id):
        company_id = _required(company_id, "company_id")
        tenant_id = await session.scalar(sa.select(Tenant.id).where(Tenant.company_id == company_id))
        if tenant_id is None:
            raise ValueError("Unknown tenant")
        return tenant_id

    @staticmethod
    def _payload(payload, company_id):
        if not isinstance(payload, dict):
            raise ValueError("Payload must be an object")
        if "claim_token" in payload:
            raise ValueError("Claim token must not enter an external payload")
        if "company_id" in payload and payload["company_id"] != company_id:
            raise ValueError("Payload tenant mismatch")

    async def _enqueue(self, session, records, *, lead_id=None, company_id=None, event_type=None,
                       session_id=None):
        for record in records:
            cid = record.get("company_id")
            if company_id is not None and cid != company_id:
                raise ValueError("Lead/outbox tenant mismatch")
            if event_type is not None and record.get("event_type") != event_type:
                raise ValueError("Lead/outbox event mismatch")
            if session_id is not None and record.get("session_id") != session_id:
                raise ValueError("Lead/outbox session mismatch")
            tenant_id = await self._tenant(session, cid)
            self._payload(record.get("payload"), cid)
            if record.get("status", "pending") != "pending" or record.get("attempts", 0) != 0:
                raise ValueError("Only new pending deliveries can be enqueued")
            if record.get("lead_id") is not None and record["lead_id"] != lead_id:
                raise ValueError("Unexpected lead reference")
            values = dict(
                delivery_id=_required(record.get("delivery_id"), "delivery_id"),
                tenant_id=tenant_id, lead_id=lead_id,
                event_type=_required(record.get("event_type"), "event_type"),
                session_id=_required(record.get("session_id"), "session_id"),
                destination_type=_required(record.get("destination_type"), "destination_type"),
                target=record.get("target"), payload=record["payload"],
            )
            times = {k: _datetime(record[k]) for k in ("created_at", "timestamp", "next_attempt_at")
                     if record.get(k) is not None}
            if "created_at" not in times and "timestamp" in times:
                times["created_at"] = times["timestamp"]
            inserted = await session.scalar(insert(outbox).values(**values, **times)
                                            .on_conflict_do_nothing(index_elements=[outbox.c.delivery_id])
                                            .returning(outbox.c.delivery_id))
            if inserted is None:
                previous = (await session.execute(sa.select(outbox).where(
                    outbox.c.delivery_id == values["delivery_id"]))).mappings().one()
                if any(previous[k] != v for k, v in values.items()):
                    raise ValueError("Delivery ID reused for a different event")

    async def save_lead(self, *, lead_id: str, event_type: str, payload: dict,
                        records: list[dict], request_receipt=None) -> None:
        _required(lead_id, "lead_id")
        _required(event_type, "event_type")
        cid = payload.get("company_id")
        self._payload(payload, cid)
        sid = _required(payload.get("session_id"), "session_id")
        if request_receipt is not None:
            ctx, response = request_receipt
            if ctx.receipts.lead_store is not self or ctx.company_id != cid:
                raise ValueError("Request receipt store or tenant mismatch")
        async with self.sessions() as session, session.begin():
            tenant_id = await self._tenant(session, cid)
            inserted = await session.scalar(insert(leads).values(
                lead_id=lead_id, tenant_id=tenant_id, event_type=event_type, session_id=sid, payload=payload,
                **({"created_at": _datetime(payload["timestamp"])} if payload.get("timestamp") else {})
            ).on_conflict_do_nothing(index_elements=[leads.c.lead_id]).returning(leads.c.lead_id))
            if inserted is None:
                previous = (await session.execute(sa.select(leads).where(leads.c.lead_id == lead_id))).mappings().one()
                if previous["tenant_id"] != tenant_id or previous["event_type"] != event_type or previous["payload"] != payload:
                    raise ValueError("Lead ID reused for a different lead")
                # Retrying lead creation must not add destinations, even with fresh delivery IDs.
                if any(r.get("company_id") != cid or r.get("event_type") != event_type
                       or r.get("session_id") != sid for r in records):
                    raise ValueError("Lead/outbox identity mismatch")
                if request_receipt is not None:
                    await ctx.receipts.checkpoint_lead(session, ctx, lead_id, response)
                return
            await self._enqueue(session, records, lead_id=lead_id, company_id=cid,
                                event_type=event_type, session_id=sid)
            if request_receipt is not None:
                await ctx.receipts.checkpoint_lead(session, ctx, lead_id, response)

    async def enqueue(self, records: list[dict]) -> None:
        async with self.sessions() as session, session.begin():
            await self._enqueue(session, records)

    async def read_leads(self, company_id: Optional[str] = None) -> list[dict]:
        async with self.sessions() as session:
            query = sa.select(leads.c.payload).join(Tenant, Tenant.id == leads.c.tenant_id)
            if company_id is not None:
                query = query.where(leads.c.tenant_id == await self._tenant(session, company_id))
            return list((await session.scalars(query.order_by(leads.c.created_at, leads.c.lead_id))).all())

    @staticmethod
    def _record(row, *, claimed=False):
        result = {k: row[k] for k in ("delivery_id", "event_type", "session_id", "destination_type",
                  "target", "payload", "status", "attempts", "last_error", "response_status")}
        result["company_id"] = row["company_id"]
        for key in ("created_at", "timestamp", "next_attempt_at", "lease_until"):
            result[key] = row[key].isoformat() if row[key] is not None else None
        result["event_timestamp"] = result["created_at"]
        if claimed:
            result["claim_token"] = str(row["claim_token"])
        return result

    async def outbox_records(self, company_id: Optional[str] = None) -> list[dict]:
        async with self.sessions() as session:
            query = sa.select(outbox, Tenant.company_id).join(Tenant, Tenant.id == outbox.c.tenant_id)
            if company_id is not None:
                query = query.where(outbox.c.tenant_id == await self._tenant(session, company_id))
            rows = (await session.execute(query.order_by(outbox.c.created_at, outbox.c.delivery_id))).mappings()
            return [self._record(row) for row in rows]

    async def claim(self, *, company_id: Optional[str] = None, limit=50, lease_seconds=120) -> list[dict]:
        if not isinstance(limit, int) or limit < 1 or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("Positive integer limit and lease_seconds required")
        async with self.sessions() as session, session.begin():
            scope = []
            if company_id is not None:
                scope.append(outbox.c.tenant_id == await self._tenant(session, company_id))
            now = sa.func.clock_timestamp()
            due = sa.or_(sa.and_(outbox.c.status.in_(["pending", "failed"]),
                                sa.or_(outbox.c.next_attempt_at.is_(None), outbox.c.next_attempt_at <= now)),
                         sa.and_(outbox.c.status == "processing", outbox.c.lease_until <= now))
            # Bounded cleanup cannot hide claimable work behind exhausted leases.
            exhausted = (sa.select(outbox.c.delivery_id).where(*scope, due, outbox.c.attempts >= 5)
                         .order_by(outbox.c.created_at, outbox.c.delivery_id).limit(50)
                         .with_for_update(skip_locked=True))
            await session.execute(outbox.update().where(outbox.c.delivery_id.in_(exhausted)).values(
                status="dead", lease_until=None, claim_token=None, next_attempt_at=None, timestamp=now,
                last_error=sa.case((outbox.c.status == "processing", "lease_expired"), else_=outbox.c.last_error)))
            query = (sa.select(outbox, Tenant.company_id).join(Tenant, Tenant.id == outbox.c.tenant_id)
                     .where(*scope, due, outbox.c.attempts < 5).order_by(outbox.c.created_at, outbox.c.delivery_id)
                     .limit(limit).with_for_update(skip_locked=True, of=outbox))
            rows = (await session.execute(query)).mappings().all()
            result = []
            for row in rows:
                updated = (await session.execute(outbox.update().where(
                    outbox.c.delivery_id == row["delivery_id"]).values(
                    status="processing", attempts=outbox.c.attempts + 1, claim_token=uuid4(),
                    lease_until=now + timedelta(seconds=lease_seconds), timestamp=now,
                    next_attempt_at=None).returning(*outbox.c))).mappings().one()
                result.append(self._record({**updated, "company_id": row["company_id"]}, claimed=True))
            return result

    async def finish(self, record: dict) -> bool:
        status = record.get("status")
        if status not in {"failed", "sent", "dead"}:
            raise ValueError("Invalid delivery result status")
        try:
            token = UUID(str(record["claim_token"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        async with self.sessions() as session, session.begin():
            tenant_id = await self._tenant(session, record.get("company_id"))
            # Database time fences late workers even when nobody has reclaimed yet.
            query = outbox.update().where(
                outbox.c.delivery_id == record.get("delivery_id"), outbox.c.tenant_id == tenant_id,
                outbox.c.status == "processing", outbox.c.claim_token == token,
                outbox.c.lease_until > sa.func.clock_timestamp())
            final_status = sa.case((sa.and_(status == "failed", outbox.c.attempts >= 5), "dead"), else_=status)
            next_at = _datetime(record.get("next_attempt_at")) if status == "failed" else None
            updated = await session.scalar(query.values(
                status=final_status, lease_until=None, claim_token=None, timestamp=sa.func.clock_timestamp(),
                next_attempt_at=sa.cast(sa.case((outbox.c.attempts >= 5, None), else_=next_at),
                                        sa.DateTime(timezone=True)),
                last_error=record.get("last_error"), response_status=record.get("response_status")
            ).returning(outbox.c.delivery_id))
            return updated is not None
