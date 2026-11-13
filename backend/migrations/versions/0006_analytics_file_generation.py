"""Identify events belonging to the last synchronized JSONL version."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0006_analytics_file_generation"
down_revision = "0005_analytics_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics_events", sa.Column("file_generation", UUID(as_uuid=True)))


def downgrade():
    op.drop_column("analytics_events", "file_generation")
