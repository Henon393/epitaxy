"""Crée audit_log append-only

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])

    # Barrière 1, privilèges : annule le default privilege de l'étape 1
    # (arwd) pour ne laisser à app_user que INSERT et SELECT. Exécutable ici
    # car migrator est le grantor de ces privilèges. TRUNCATE n'a jamais été
    # accordé (arwd ne contient pas D).
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM app_user")

    # Barrière 2, RLS par commande : policies SELECT et INSERT scopées au
    # tenant courant, AUCUNE policy UPDATE/DELETE. Sous FORCE ROW LEVEL
    # SECURITY, l'absence de policy vaut refus, même si un privilège
    # réapparaissait un jour par erreur, la RLS bloquerait encore.
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_select ON audit_log FOR SELECT
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY audit_insert ON audit_log FOR INSERT
        WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
