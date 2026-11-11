"""Read-only SQL reports: project public metadata, never transfer JSON documents."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from ..db.models import Tenant
from ..db.lead_models import leads, delivery_outbox as outbox


def _utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value, fallback):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return _utc(parsed).replace(tzinfo=None).isoformat() if parsed.tzinfo else value
        except ValueError:
            pass
    return _utc(fallback).replace(tzinfo=None).isoformat()


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Non-negative integer limit required")
    return value


class PostgresReports:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _lead_columns():
        return [Tenant.company_id, leads.c.session_id, leads.c.created_at,
                *[leads.c.payload[key].astext.label(key) for key in (
                    "timestamp", "service_id", "reason", "lead_trigger", "preferred_time", "page")],
                leads.c.payload["needs_operator"].as_boolean().label("needs_operator")]

    @staticmethod
    def _metadata(row):
        result = dict(row)
        result["timestamp"] = _timestamp(result.get("timestamp"), result.pop("created_at"))
        return result

    async def lead_metadata(self, company_id=None) -> list[dict]:
        async with self.store.sessions() as session:
            query = sa.select(*self._lead_columns()).join(Tenant, Tenant.id == leads.c.tenant_id)
            if company_id is not None:
                query = query.where(leads.c.tenant_id == await self.store._tenant(session, company_id))
            rows = (await session.execute(query.order_by(leads.c.created_at, leads.c.lead_id))).mappings()
            return [self._metadata(row) for row in rows]

    async def leads_feed(self, *, company_id=None, days=None, start=None, end=None,
                         reason=None, limit=200) -> list[dict]:
        limit = _limit(limit)
        if start is None and end is None and days is not None:
            start = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.store.sessions() as session:
            query = sa.select(*self._lead_columns()).join(Tenant, Tenant.id == leads.c.tenant_id)
            if company_id is not None:
                query = query.where(leads.c.tenant_id == await self.store._tenant(session, company_id))
            if start is not None:
                query = query.where(leads.c.created_at >= _utc(start))
            if end is not None:
                query = query.where(leads.c.created_at <= _utc(end))
            if reason is not None:
                reason_value = sa.func.coalesce(sa.func.nullif(leads.c.payload["reason"].astext, ""),
                                                "commercial_interest")
                query = query.where(reason_value == reason)
            query = query.order_by(leads.c.created_at.desc(), leads.c.lead_id.desc()).limit(limit)
            rows = (await session.execute(query)).mappings()
            result = []
            for row in rows:
                item = self._metadata(row)
                item.pop("company_id")
                item["reason"] = str(item["reason"] or "commercial_interest")
                item["needs_operator"] = bool(item["needs_operator"])
                item["lead_trigger"] = str(item["lead_trigger"] or "ask_contact")
                for key in ("preferred_time", "page"):
                    item[key] = str(item[key] or "")
                result.append(item)
            return result

    async def outbox_summary(self, company_id=None, limit=50) -> dict:
        limit = _limit(limit)
        async with self.store.sessions() as session:
            scope = []
            if company_id is not None:
                scope.append(outbox.c.tenant_id == await self.store._tenant(session, company_id))
            by_status = dict((await session.execute(sa.select(outbox.c.status, sa.func.count())
                             .where(*scope).group_by(outbox.c.status))).all())
            by_destination = dict((await session.execute(sa.select(outbox.c.destination_type, sa.func.count())
                                  .where(*scope).group_by(outbox.c.destination_type))).all())
            keys = ("timestamp", "delivery_id", "session_id", "destination_type", "status", "attempts",
                    "next_attempt_at", "last_error", "response_status")
            query = (sa.select(Tenant.company_id, *[outbox.c[key] for key in keys])
                     .join(Tenant, Tenant.id == outbox.c.tenant_id).where(*scope)
                     .order_by(outbox.c.timestamp.desc(), outbox.c.delivery_id.desc()).limit(limit))
            recent = []
            for row in (await session.execute(query)).mappings():
                item = dict(row)
                for key in ("timestamp", "next_attempt_at"):
                    item[key] = item[key].isoformat() if item[key] is not None else None
                recent.append(item)
            return {"company_id": company_id, "total": sum(by_status.values()),
                    "by_status": by_status, "by_destination": by_destination, "recent": recent}

    async def outbox_health(self) -> dict:
        now = sa.func.now()
        active = outbox.c.status.in_(("pending", "failed", "processing"))
        due = sa.case((outbox.c.status == "processing", outbox.c.lease_until),
                      else_=sa.func.coalesce(outbox.c.next_attempt_at, outbox.c.timestamp))
        query = sa.select(
            sa.func.count().filter(active).label("pending_events"),
            sa.func.count().filter(sa.and_(outbox.c.status == "dead",
                outbox.c.timestamp >= now - timedelta(days=7))).label("dead_events"),
            sa.func.count().filter(outbox.c.status == "dead").label("dead_events_total"),
            sa.func.greatest(0, sa.func.coalesce(sa.extract("epoch", now -
                sa.func.min(due).filter(active)), 0)).label("oldest_due_seconds"))
        async with self.store.sessions() as session:
            row = (await session.execute(query)).mappings().one()
        result = {key: int(value) for key, value in row.items()}
        result["status"] = "degraded" if result["dead_events"] or result["oldest_due_seconds"] >= 300 else "ok"
        if result["oldest_due_seconds"] >= 300:
            result["detail"] = "Очередь БД отстаёт более чем на 5 минут"
        result["storage"] = "postgres"
        return result

    async def readiness(self) -> dict:
        """Observe prerequisites only; no probe can prove future disk/write success."""
        async with self.store.sessions() as session:
            rows = (await session.execute(sa.text("""
                SELECT table_name, privilege,
                       has_table_privilege(current_user, 'public.' || table_name, privilege) AS allowed
                FROM (VALUES ('tenants','SELECT'), ('leads','SELECT'), ('leads','INSERT'),
                    ('delivery_outbox','SELECT'), ('delivery_outbox','INSERT'), ('delivery_outbox','UPDATE'),
                    ('chat_requests','SELECT'), ('chat_requests','INSERT'), ('chat_requests','UPDATE'))
                    AS required(table_name, privilege)
            """))).mappings().all()
            read_only = await session.scalar(sa.text("SHOW transaction_read_only")) == "on"
        permissions = {}
        for row in rows:
            permissions.setdefault(row["table_name"], {})[row["privilege"]] = row["allowed"]
        # Missing SELECT must follow the same safe error path as an actual failed read.
        if any(not rights["SELECT"] for rights in permissions.values()):
            raise PermissionError("Lead storage SELECT permission missing")
        missing = [table for table, rights in permissions.items() if not all(rights.values())]
        writable = not read_only and not missing
        return {"status": "ok" if writable else "degraded", "writable": bool(writable),
                "read_only": read_only, "permissions": permissions, "missing_permissions": missing}

    async def lead_health(self) -> dict:
        readiness = await self.readiness()
        async with self.store.sessions() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(leads))
            latest = (await session.execute(sa.select(leads.c.created_at, Tenant.company_id)
                      .join(Tenant, Tenant.id == leads.c.tenant_id)
                      .order_by(leads.c.created_at.desc(), leads.c.lead_id.desc()).limit(1))).first()
        return {"count": count, "last_lead_at": latest.created_at.isoformat() if latest else None,
                "last_lead_company": latest.company_id if latest else None,
                "writable": readiness["writable"], "read_only": readiness["read_only"],
                "missing_permissions": readiness["missing_permissions"]}
