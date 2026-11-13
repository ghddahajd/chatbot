"""Event fields used by the conversion funnel."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlmodel import SQLModel


analytics_events = sa.Table(
    "analytics_events", SQLModel.metadata,
    sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
    sa.Column("event_key", sa.Text, primary_key=True),
    sa.Column("file_generation", UUID(as_uuid=True)),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text),
    sa.Column("visitor_id", sa.Text),
    sa.Column("page", sa.Text),
    sa.Column("file_position", sa.BigInteger),
    sa.Column("question", sa.Text),
    sa.Column("question_key", sa.Text),
    sa.Column("category", sa.Text),
    sa.Column("claimed_by", sa.Text),
    sa.Column("unanswered", JSONB(none_as_null=True)),
)
sa.Index("ix_analytics_events_tenant_time", analytics_events.c.tenant_id, analytics_events.c.occurred_at)
