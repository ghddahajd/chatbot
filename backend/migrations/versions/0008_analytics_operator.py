"""Store operator labels for the handover reports."""

from alembic import op
import sqlalchemy as sa

revision = "0008_analytics_operator"
down_revision = "0007_analytics_question_fields"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics_events", sa.Column("claimed_by", sa.Text))


def downgrade():
    op.drop_column("analytics_events", "claimed_by")
