"""Keep the fields shown in recent unanswered questions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_analytics_unanswered"
down_revision = "0008_analytics_operator"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics_events", sa.Column("unanswered", JSONB(none_as_null=True)))


def downgrade():
    op.drop_column("analytics_events", "unanswered")
