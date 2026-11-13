"""Analytics events for offline funnel comparison."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0005_analytics_events"
down_revision = "0004_chat_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analytics_events",
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("snapshot_sha256", sa.String(64), primary_key=True),
        sa.Column("line_number", sa.BigInteger, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text),
        sa.Column("visitor_id", sa.Text),
        sa.Column("page", sa.Text),
    )
    op.create_index("ix_analytics_events_tenant_time", "analytics_events", ["tenant_id", "occurred_at"])


def downgrade():
    op.drop_table("analytics_events")
