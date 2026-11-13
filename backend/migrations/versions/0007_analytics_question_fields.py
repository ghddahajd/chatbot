"""Fields used by question and category reports."""

from alembic import op
import sqlalchemy as sa

revision = "0007_analytics_question_fields"
down_revision = "0006_analytics_file_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics_events", sa.Column("file_position", sa.BigInteger))
    for name in ("question", "question_key", "category"):
        op.add_column("analytics_events", sa.Column(name, sa.Text))


def downgrade():
    for name in ("category", "question_key", "question", "file_position"):
        op.drop_column("analytics_events", name)
