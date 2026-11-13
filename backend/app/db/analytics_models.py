"""Event fields used by the conversion funnel."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
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
)
sa.Index("ix_analytics_events_tenant_time", analytics_events.c.tenant_id, analytics_events.c.occurred_at)
