"""Durable request receipts, without retention or runtime activation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004_chat_requests"
down_revision = "0003_leads_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_requests",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("request_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("claim_token", UUID(as_uuid=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="processing"),
        sa.Column("response", JSONB(none_as_null=True)),
        sa.Column("lead_id", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "lead_id"], ["leads.tenant_id", "leads.lead_id"],
                                name="fk_chat_requests_tenant_lead"),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_chat_requests_fingerprint"),
        sa.CheckConstraint("status IN ('processing','done','uncertain')", name="ck_chat_requests_status"),
        sa.CheckConstraint("response IS NULL OR jsonb_typeof(response) = 'object'", name="ck_chat_requests_response"),
        sa.CheckConstraint("status <> 'done' OR response IS NOT NULL", name="ck_chat_requests_done"),
        sa.CheckConstraint("lead_id IS NULL OR response IS NOT NULL", name="ck_chat_requests_lead_response"),
    )
    op.create_index("ix_chat_requests_tenant_lead", "chat_requests", ["tenant_id", "lead_id"],
                    postgresql_where=sa.text("lead_id IS NOT NULL"))


def downgrade():
    op.drop_table("chat_requests")
