"""Keep historical analytics company IDs independent of active registrations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0011_analytics_company_scope"
down_revision = "0010_analytics_rollup"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics_events", sa.Column("company_id", sa.Text))
    op.execute("""
        UPDATE analytics_events AS e SET company_id = t.company_id
        FROM tenants AS t WHERE t.id = e.tenant_id
    """)
    op.alter_column("analytics_events", "company_id", nullable=False)
    op.drop_constraint("analytics_events_pkey", "analytics_events", type_="primary")
    op.drop_index("ix_analytics_events_tenant_time", table_name="analytics_events")
    op.drop_column("analytics_events", "tenant_id")
    op.create_primary_key("analytics_events_pkey", "analytics_events", ["company_id", "event_key"])
    op.create_index("ix_analytics_events_company_time", "analytics_events", ["company_id", "occurred_at"])


def downgrade():
    # The old schema cannot represent unregistered companies. NOT NULL makes
    # this downgrade fail transactionally if any such events are present.
    op.add_column("analytics_events", sa.Column("tenant_id", UUID(as_uuid=True)))
    op.execute("""
        UPDATE analytics_events AS e SET tenant_id = t.id
        FROM tenants AS t WHERE t.company_id = e.company_id
    """)
    op.alter_column("analytics_events", "tenant_id", nullable=False)
    op.create_foreign_key("analytics_events_tenant_id_fkey", "analytics_events", "tenants", ["tenant_id"], ["id"])
    op.drop_constraint("analytics_events_pkey", "analytics_events", type_="primary")
    op.drop_index("ix_analytics_events_company_time", table_name="analytics_events")
    op.drop_column("analytics_events", "company_id")
    op.create_primary_key("analytics_events_pkey", "analytics_events", ["tenant_id", "event_key"])
    op.create_index("ix_analytics_events_tenant_time", "analytics_events", ["tenant_id", "occurred_at"])
