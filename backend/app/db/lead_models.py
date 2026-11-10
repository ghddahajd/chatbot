"""Metadata for immutable leads and a tenant-bound generic delivery queue."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlmodel import SQLModel

leads = sa.Table(
    "leads", SQLModel.metadata,
    sa.Column("lead_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("tenant_id", "lead_id", name="uq_leads_tenant_lead"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_leads_payload"),
)
sa.Index("ix_leads_tenant_created", leads.c.tenant_id, leads.c.created_at)
sa.Index("ix_leads_session_id", leads.c.session_id)

delivery_outbox = sa.Table(
    "delivery_outbox", SQLModel.metadata,
    sa.Column("delivery_id", sa.Text, primary_key=True),
    sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
    sa.Column("lead_id", sa.Text),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("destination_type", sa.Text, nullable=False),
    sa.Column("target", sa.Text),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("claim_token", UUID(as_uuid=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("response_status", sa.Integer),
    sa.ForeignKeyConstraint(["tenant_id", "lead_id"], ["leads.tenant_id", "leads.lead_id"],
                            name="fk_outbox_tenant_lead"),
    sa.CheckConstraint("status IN ('pending','processing','failed','sent','dead')", name="ck_outbox_status"),
    sa.CheckConstraint("attempts BETWEEN 0 AND 5", name="ck_outbox_attempts"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_outbox_payload"),
    sa.CheckConstraint("(status = 'processing' AND lease_until IS NOT NULL AND claim_token IS NOT NULL) "
                       "OR (status <> 'processing' AND lease_until IS NULL AND claim_token IS NULL)",
                       name="ck_outbox_lease"),
)
sa.Index("ix_outbox_tenant_lead", delivery_outbox.c.tenant_id, delivery_outbox.c.lead_id)
sa.Index("ix_outbox_created_at", delivery_outbox.c.created_at)
sa.Index("ix_outbox_session_id", delivery_outbox.c.session_id)
sa.Index("ix_outbox_due", delivery_outbox.c.next_attempt_at,
         postgresql_where=sa.text("status IN ('pending','failed')"))
sa.Index("ix_outbox_lease", delivery_outbox.c.lease_until,
         postgresql_where=sa.text("status = 'processing'"))
