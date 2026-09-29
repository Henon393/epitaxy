"""Crée les transmissions PA append-only

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _append_only(table: str) -> None:
    """Pattern audit : DML restreint à INSERT/SELECT, RLS par commande."""
    op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM app_user")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_select ON {table} FOR SELECT
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )
    op.execute(
        f"""
        CREATE POLICY {table}_insert ON {table} FOR INSERT
        WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        "pa_transmissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("pa_transmission_ref", sa.String(length=64), nullable=False),
        sa.Column("routing_identifier", sa.String(length=14), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pa_transmissions_tenant_id", "pa_transmissions", ["tenant_id"])
    op.create_index("ix_pa_transmissions_invoice_id", "pa_transmissions", ["invoice_id"])
    _append_only("pa_transmissions")

    op.create_table(
        "pa_status_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("transmission_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("paid_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("paid_at", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["transmission_id"], ["pa_transmissions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transmission_id", "position", name="uq_pa_status_events_position"),
    )
    op.create_index("ix_pa_status_events_tenant_id", "pa_status_events", ["tenant_id"])
    op.create_index("ix_pa_status_events_transmission_id", "pa_status_events", ["transmission_id"])
    _append_only("pa_status_events")

    # Barrière base de la machine à états : le MÊME graphe que app/pa.py.
    # Progression avant seulement (recommandés sautables), rejetee sortant
    # de deposee uniquement, rejetee/refusee puits terminaux, encaissee
    # répétable avec montant et date obligatoires, positions contiguës
    # (l'unicité (transmission_id, position) sérialise la concurrence).
    op.execute(
        """
        CREATE FUNCTION enforce_pa_status_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            prev record;
        BEGIN
            IF NEW.status = 'encaissee'
               AND (NEW.paid_amount IS NULL OR NEW.paid_at IS NULL) THEN
                RAISE EXCEPTION
                    'statut encaissee : montant et date de paiement obligatoires';
            END IF;
            SELECT status, position INTO prev FROM pa_status_events
                WHERE transmission_id = NEW.transmission_id
                ORDER BY position DESC LIMIT 1;
            IF prev IS NULL THEN
                IF NEW.position <> 1 THEN
                    RAISE EXCEPTION 'premier evenement : position 1 attendue';
                END IF;
                IF NEW.status <> 'deposee' THEN
                    RAISE EXCEPTION 'premier statut obligatoirement deposee';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.position <> prev.position + 1 THEN
                RAISE EXCEPTION 'position % non contigue (attendue %)',
                    NEW.position, prev.position + 1;
            END IF;
            IF NOT (
                (prev.status = 'deposee' AND NEW.status IN
                    ('recue', 'approuvee', 'encaissee', 'refusee', 'rejetee'))
                OR (prev.status = 'recue' AND NEW.status IN
                    ('approuvee', 'encaissee', 'refusee'))
                OR (prev.status = 'approuvee' AND NEW.status = 'encaissee')
                OR (prev.status = 'encaissee' AND NEW.status = 'encaissee')
            ) THEN
                RAISE EXCEPTION 'transition de statut invalide : % -> %',
                    prev.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER pa_status_events_transitions
        BEFORE INSERT ON pa_status_events
        FOR EACH ROW EXECUTE FUNCTION enforce_pa_status_transition()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER pa_status_events_transitions ON pa_status_events")
    op.execute("DROP FUNCTION enforce_pa_status_transition()")
    op.drop_table("pa_status_events")
    op.drop_table("pa_transmissions")
