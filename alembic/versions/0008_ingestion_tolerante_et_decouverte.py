"""Ingestion tolérante et découverte des transmissions actives

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-08

Bascule gardien vers enregistreur (5b) : les statuts rapportés par la PA
font foi. Une séquence hors du graphe attendu est consignée avec
out_of_graph = true et signalée, plus jamais rejetée. Le trigger v2 exige
la cohérence du flag DANS LES DEUX SENS : hors graphe non déclaré = rejet
(un bug ne maquille pas une déviation en normal), déclaré sur une
transition valide = rejet (toute anomalie signalée est un vrai positif).

Découverte base-first : la fonction SECURITY DEFINER pa_active_transmissions,
propriété du rôle NOLOGIN pa_scanner (créé par init-roles.sh), ne retourne
que des paires (tenant_id, transmission_id). La RLS d'app_user n'est ni
désactivée ni affaiblie : pa_scanner reçoit des policies ciblées en lecture
seule sur les trois tables strictement nécessaires.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = 'pa_scanner'")).scalar()
        is None
    ):
        raise RuntimeError(
            "Rôle pa_scanner manquant (créé par docker/postgres/init-roles.sh sur un "
            "volume neuf). Sur un volume existant, exécuter une fois en superuser : "
            "CREATE ROLE pa_scanner NOLOGIN NOBYPASSRLS; GRANT pa_scanner TO migrator; "
            "GRANT USAGE, CREATE ON SCHEMA public TO pa_scanner;"
        )

    # --- Idempotence et anomalies ------------------------------------------
    op.add_column(
        "pa_status_events", sa.Column("pa_event_ref", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "pa_status_events",
        sa.Column("out_of_graph", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index(
        "uq_pa_status_events_event_ref",
        "pa_status_events",
        ["transmission_id", "pa_event_ref"],
        unique=True,
        postgresql_where=sa.text("pa_event_ref IS NOT NULL"),
    )

    # --- Trigger v2 : graphe conditionnel, flag vérifié des deux côtés ------
    # Inconditionnel : positions contiguës, premier événement deposee (c'est
    # notre soumission), montant + date sur encaissee, append-only (REVOKE et
    # RLS inchangés).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_pa_status_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            prev record;
            valid boolean;
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
                IF NEW.out_of_graph THEN
                    RAISE EXCEPTION
                        'anomalie declaree sur une transition valide : (depot initial)';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.position <> prev.position + 1 THEN
                RAISE EXCEPTION 'position % non contigue (attendue %)',
                    NEW.position, prev.position + 1;
            END IF;
            valid := (
                (prev.status = 'deposee' AND NEW.status IN
                    ('recue', 'approuvee', 'encaissee', 'refusee', 'rejetee'))
                OR (prev.status = 'recue' AND NEW.status IN
                    ('approuvee', 'encaissee', 'refusee'))
                OR (prev.status = 'approuvee' AND NEW.status = 'encaissee')
                OR (prev.status = 'encaissee' AND NEW.status = 'encaissee')
            );
            IF NOT valid AND NOT NEW.out_of_graph THEN
                RAISE EXCEPTION
                    'transition de statut invalide : % -> % (hors graphe non declare)',
                    prev.status, NEW.status;
            END IF;
            IF valid AND NEW.out_of_graph THEN
                RAISE EXCEPTION
                    'anomalie declaree sur une transition valide : % -> %',
                    prev.status, NEW.status;
            END IF;
            RETURN NEW;
        END $$
        """
    )

    # --- Découverte base-first ----------------------------------------------
    op.execute("GRANT SELECT ON pa_transmissions, pa_status_events, invoices TO pa_scanner")
    for table in ("pa_transmissions", "pa_status_events", "invoices"):
        op.execute(
            f"""
            CREATE POLICY {table}_pa_scanner_scan ON {table}
            FOR SELECT TO pa_scanner USING (true)
            """
        )
    # Active = dernier statut non terminal ET encaissements non soldés.
    # Ne retourne QUE des identifiants, aucune donnée métier.
    op.execute(
        """
        CREATE FUNCTION pa_active_transmissions()
        RETURNS TABLE (tenant_id uuid, transmission_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT t.tenant_id, t.id
            FROM pa_transmissions t
            JOIN LATERAL (
                SELECT e.status FROM pa_status_events e
                WHERE e.transmission_id = t.id
                ORDER BY e.position DESC LIMIT 1
            ) last ON true
            JOIN invoices i ON i.id = t.invoice_id
            WHERE last.status NOT IN ('rejetee', 'refusee')
              AND COALESCE((
                    SELECT sum(e2.paid_amount) FROM pa_status_events e2
                    WHERE e2.transmission_id = t.id AND e2.status = 'encaissee'
                  ), 0) < i.total_ttc
        $$
        """
    )
    # Le transfert de propriété exige que pa_scanner ait CREATE sur le
    # schéma — accordé par le superuser dans init-roles.sh (migrator ne peut
    # pas l'accorder lui-même, il n'est pas propriétaire du schéma).
    op.execute("ALTER FUNCTION pa_active_transmissions() OWNER TO pa_scanner")
    op.execute("REVOKE ALL ON FUNCTION pa_active_transmissions() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION pa_active_transmissions() TO app_user")


def downgrade() -> None:
    op.execute("DROP FUNCTION pa_active_transmissions()")
    for table in ("pa_transmissions", "pa_status_events", "invoices"):
        op.execute(f"DROP POLICY {table}_pa_scanner_scan ON {table}")
    op.execute("REVOKE SELECT ON pa_transmissions, pa_status_events, invoices FROM pa_scanner")
    op.drop_index("uq_pa_status_events_event_ref", table_name="pa_status_events")
    op.drop_column("pa_status_events", "out_of_graph")
    op.drop_column("pa_status_events", "pa_event_ref")
    # Trigger v1 non restauré : voir la migration 0006 pour la version stricte.
