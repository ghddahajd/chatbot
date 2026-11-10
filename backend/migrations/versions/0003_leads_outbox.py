"""Immutable leads and an atomic generic outbox, without runtime activation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003_leads_outbox"
down_revision = "0002_client_access"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "leads",
        sa.Column("lead_id", sa.Text, primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "lead_id", name="uq_leads_tenant_lead"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_leads_payload"),
    )
    op.create_index("ix_leads_tenant_created", "leads", ["tenant_id", "created_at"])
    op.create_index("ix_leads_session_id", "leads", ["session_id"])
    op.create_table(
        "delivery_outbox",
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
    op.create_index("ix_outbox_tenant_lead", "delivery_outbox", ["tenant_id", "lead_id"])
    op.create_index("ix_outbox_created_at", "delivery_outbox", ["created_at"])
    op.create_index("ix_outbox_session_id", "delivery_outbox", ["session_id"])
    op.create_index("ix_outbox_due", "delivery_outbox", ["next_attempt_at"],
                    postgresql_where=sa.text("status IN ('pending','failed')"))
    op.create_index("ix_outbox_lease", "delivery_outbox", ["lease_until"],
                    postgresql_where=sa.text("status = 'processing'"))


def downgrade():
    op.drop_table("delivery_outbox")
    op.drop_table("leads")
