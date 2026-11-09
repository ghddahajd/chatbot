"""Client registry, independent from current runtime stores."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_client_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", name="uq_tenants_company_id"),
        sa.CheckConstraint("company_id ~ '^[A-Za-z0-9_-]+$'", name="ck_tenants_company_id"),
        sa.CheckConstraint("length(trim(company_name)) > 0", name="ck_tenants_company_name"),
    )
    op.create_index("ix_tenants_created_at", "tenants", ["created_at"])


def downgrade():
    op.drop_index("ix_tenants_created_at", table_name="tenants")
    op.drop_table("tenants")
