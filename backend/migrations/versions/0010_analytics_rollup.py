"""Store a synchronized copy of historical chart counters."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0010_analytics_rollup"
down_revision = "0009_analytics_unanswered"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analytics_rollup",
        sa.Column("position", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("generation", UUID(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Text),
        sa.Column("day_key", sa.Text, nullable=False),
        sa.Column("payload", JSONB(none_as_null=True), nullable=False),
    )
    op.create_index("ix_analytics_rollup_company_day", "analytics_rollup", ["company_id", "day_key"])


def downgrade():
    op.drop_table("analytics_rollup")
