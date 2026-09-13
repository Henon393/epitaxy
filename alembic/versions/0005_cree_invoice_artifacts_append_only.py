"""Crée invoice_artifacts append-only

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", "kind", name="uq_invoice_artifacts_invoice_kind"),
    )
    op.create_index("ix_invoice_artifacts_tenant_id", "invoice_artifacts", ["tenant_id"])

    # Même régime que audit_log : l'artefact d'une facture émise ne se
    # réécrit pas. Barrière 1, privilèges (migrator est grantor du default
    # privilege arwd, il peut le retrancher).
    op.execute("REVOKE UPDATE, DELETE ON invoice_artifacts FROM app_user")
    # Barrière 2, RLS par commande : SELECT et INSERT scopés tenant,
    # aucune policy UPDATE/DELETE, donc refus sous FORCE.
    op.execute("ALTER TABLE invoice_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invoice_artifacts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY artifact_select ON invoice_artifacts FOR SELECT
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )
    op.execute(
        """
        CREATE POLICY artifact_insert ON invoice_artifacts FOR INSERT
        WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_artifacts_tenant_id", table_name="invoice_artifacts")
    op.drop_table("invoice_artifacts")
