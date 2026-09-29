"""Crée la facturation et le profil vendeur

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_POLICY = "tenant_id = current_setting('app.tenant_id')::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({_TENANT_POLICY})")


def upgrade() -> None:
    # --- customers : SIREN et adresse structurée (EN 16931) ---------------
    op.add_column("customers", sa.Column("siren", sa.String(length=9), nullable=True))
    op.add_column("customers", sa.Column("address_line1", sa.String(length=200), nullable=True))
    op.add_column("customers", sa.Column("address_line2", sa.String(length=200), nullable=True))
    op.add_column("customers", sa.Column("postal_code", sa.String(length=10), nullable=True))
    op.add_column("customers", sa.Column("city", sa.String(length=100), nullable=True))
    op.add_column("customers", sa.Column("country_code", sa.String(length=2), nullable=True))

    # --- company_profiles --------------------------------------------------
    op.create_table(
        "company_profiles",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=False),
        sa.Column("siren", sa.String(length=9), nullable=False),
        sa.Column("siret", sa.String(length=14), nullable=True),
        sa.Column("address_line1", sa.String(length=200), nullable=False),
        sa.Column("address_line2", sa.String(length=200), nullable=True),
        sa.Column("postal_code", sa.String(length=10), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("vat_number", sa.String(length=20), nullable=True),
        sa.Column("legal_form", sa.String(length=50), nullable=False),
        sa.Column("vat_regime", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    _rls("company_profiles")

    # --- invoices -----------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("supply_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("operation_category", sa.String(length=30), nullable=False),
        sa.Column("vat_on_debits", sa.Boolean(), nullable=False),
        sa.Column("delivery_address", sa.String(length=500), nullable=True),
        sa.Column("total_ht", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_tva", sa.Numeric(12, 2), nullable=False),
        sa.Column("total_ttc", sa.Numeric(12, 2), nullable=False),
        sa.Column("vat_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("seller_legal_name", sa.String(length=200), nullable=True),
        sa.Column("seller_siren", sa.String(length=9), nullable=True),
        sa.Column("seller_siret", sa.String(length=14), nullable=True),
        sa.Column("seller_address_line1", sa.String(length=200), nullable=True),
        sa.Column("seller_address_line2", sa.String(length=200), nullable=True),
        sa.Column("seller_postal_code", sa.String(length=10), nullable=True),
        sa.Column("seller_city", sa.String(length=100), nullable=True),
        sa.Column("seller_country_code", sa.String(length=2), nullable=True),
        sa.Column("seller_vat_number", sa.String(length=20), nullable=True),
        sa.Column("seller_legal_form", sa.String(length=50), nullable=True),
        sa.Column("seller_vat_regime", sa.String(length=30), nullable=True),
        sa.Column("vat_mention", sa.String(length=100), nullable=True),
        sa.Column("buyer_name", sa.String(length=200), nullable=True),
        sa.Column("buyer_siren", sa.String(length=9), nullable=True),
        sa.Column("buyer_address_line1", sa.String(length=200), nullable=True),
        sa.Column("buyer_address_line2", sa.String(length=200), nullable=True),
        sa.Column("buyer_postal_code", sa.String(length=10), nullable=True),
        sa.Column("buyer_city", sa.String(length=100), nullable=True),
        sa.Column("buyer_country_code", sa.String(length=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])
    op.create_index(
        "uq_invoices_tenant_number",
        "invoices",
        ["tenant_id", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    _rls("invoices")

    # --- invoice_lines --------------------------------------------------------
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("designation", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price_ht", sa.Numeric(12, 4), nullable=False),
        sa.Column("vat_rate", sa.Numeric(4, 2), nullable=True),
        sa.Column("total_ht", sa.Numeric(12, 2), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invoice_lines_tenant_id", "invoice_lines", ["tenant_id"])
    op.create_index("ix_invoice_lines_invoice_id", "invoice_lines", ["invoice_id"])
    _rls("invoice_lines")

    # --- invoice_counters -----------------------------------------------------
    op.create_table(
        "invoice_counters",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    _rls("invoice_counters")

    # --- Triggers d'immuabilité (barrière base, détenue par migrator) --------
    # app_user, non propriétaire, ne peut ni les supprimer ni les désactiver.
    op.execute(
        """
        CREATE FUNCTION forbid_mutation_of_issued_invoice() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'emise' THEN
                    RAISE EXCEPTION 'facture emise immuable : suppression interdite';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.status = 'emise' THEN
                RAISE EXCEPTION 'facture emise immuable : modification interdite';
            END IF;
            IF OLD.number IS NOT NULL AND NEW.number IS DISTINCT FROM OLD.number THEN
                RAISE EXCEPTION 'numero de facture immuable : reattribution interdite';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER invoices_immutable_when_issued
        BEFORE UPDATE OR DELETE ON invoices
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation_of_issued_invoice()
        """
    )
    op.execute(
        """
        CREATE FUNCTION forbid_mutation_of_issued_invoice_lines() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            v_status text;
        BEGIN
            -- INSERT dans une facture emise, modification ou suppression d'une
            -- ligne d'une facture emise, ou deplacement de/vers une facture
            -- emise : tout est refuse. Les deux parents (OLD et NEW) sont
            -- verifies pour couvrir le deplacement de ligne.
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT status INTO v_status FROM invoices WHERE id = OLD.invoice_id;
                IF v_status = 'emise' THEN
                    RAISE EXCEPTION 'facture emise immuable : lignes intouchables';
                END IF;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT status INTO v_status FROM invoices WHERE id = NEW.invoice_id;
                IF v_status = 'emise' THEN
                    RAISE EXCEPTION 'facture emise immuable : lignes intouchables';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER invoice_lines_immutable_when_issued
        BEFORE INSERT OR UPDATE OR DELETE ON invoice_lines
        FOR EACH ROW EXECUTE FUNCTION forbid_mutation_of_issued_invoice_lines()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER invoice_lines_immutable_when_issued ON invoice_lines")
    op.execute("DROP FUNCTION forbid_mutation_of_issued_invoice_lines()")
    op.execute("DROP TRIGGER invoices_immutable_when_issued ON invoices")
    op.execute("DROP FUNCTION forbid_mutation_of_issued_invoice()")
    op.drop_table("invoice_counters")
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
    op.drop_table("company_profiles")
    for column in (
        "country_code",
        "city",
        "postal_code",
        "address_line2",
        "address_line1",
        "siren",
    ):
        op.drop_column("customers", column)
