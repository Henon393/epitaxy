"""Ajoute le MFA TOTP

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Anti-rejeu TOTP et compteur d'invalidation de sessions.
    op.add_column("users", sa.Column("mfa_last_timestep", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )

    op.create_table(
        "mfa_backup_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mfa_backup_codes_tenant_id", "mfa_backup_codes", ["tenant_id"])
    op.create_index("ix_mfa_backup_codes_user_id", "mfa_backup_codes", ["user_id"])
    op.execute("ALTER TABLE mfa_backup_codes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mfa_backup_codes FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON mfa_backup_codes
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )


def downgrade() -> None:
    op.drop_table("mfa_backup_codes")
    op.drop_column("users", "session_version")
    op.drop_column("users", "mfa_last_timestep")
