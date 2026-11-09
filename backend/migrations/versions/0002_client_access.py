"""Personal clinic accounts and revocable login sessions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_client_access"
down_revision = "0001_client_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("login", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("auth_version", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("login", name="uq_client_accounts_login"),
        sa.CheckConstraint("login ~ '^[a-z0-9][a-z0-9_.@-]{2,63}$'", name="ck_client_accounts_login"),
    )
    op.create_index("ix_client_accounts_tenant_id", "client_accounts", ["tenant_id"])
    op.create_table(
        "client_login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("client_accounts.id"), nullable=False),
        sa.Column("auth_version", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_client_login_sessions_account_id", "client_login_sessions", ["account_id"])
    op.create_index("ix_client_login_sessions_expires_at", "client_login_sessions", ["expires_at"])


def downgrade():
    op.drop_table("client_login_sessions")
    op.drop_table("client_accounts")
