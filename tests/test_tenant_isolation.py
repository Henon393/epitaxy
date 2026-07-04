"""Tests d'isolation tenant — la RLS elle-même, via le rôle app_user.

Ces tests attaquent la base en SQL direct, sans passer par l'API : ils
prouvent que l'isolation tient au niveau PostgreSQL, dernier rempart si la
couche applicative laissait passer une requête mal filtrée.
"""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import SeedData, set_tenant

SELECT_CUSTOMERS = text("SELECT id, tenant_id, name FROM customers")


def test_select_ne_retourne_que_les_lignes_du_tenant(app_engine: Engine, seed: SeedData) -> None:
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        rows = conn.execute(SELECT_CUSTOMERS).fetchall()
    assert [row.id for row in rows] == [seed.customer_a]


def test_update_croise_n_affecte_aucune_ligne(app_engine: Engine, seed: SeedData) -> None:
    # Le tenant A tente de modifier la ligne du tenant B en la ciblant par id :
    # la RLS la rend invisible, l'UPDATE ne touche rien.
    with app_engine.begin() as conn:
        set_tenant(conn, seed.tenant_a)
        result = conn.execute(
            text("UPDATE customers SET name = 'pirate' WHERE id = :id"),
            {"id": seed.customer_b},
        )
        assert result.rowcount == 0

    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_b)
        name = conn.execute(
            text("SELECT name FROM customers WHERE id = :id"), {"id": seed.customer_b}
        ).scalar_one()
    assert name == "Client B"


def test_delete_croise_n_affecte_aucune_ligne(app_engine: Engine, seed: SeedData) -> None:
    with app_engine.begin() as conn:
        set_tenant(conn, seed.tenant_a)
        result = conn.execute(text("DELETE FROM customers WHERE id = :id"), {"id": seed.customer_b})
        assert result.rowcount == 0


def test_insert_pour_un_autre_tenant_rejete(app_engine: Engine, seed: SeedData) -> None:
    # La policy sans WITH CHECK explicite applique l'expression USING aux
    # écritures : écrire une ligne au nom d'un autre tenant est une violation.
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(DBAPIError, match="row-level security"):
            conn.execute(
                text(
                    "INSERT INTO customers (id, tenant_id, name, email) "
                    "VALUES (gen_random_uuid(), :tenant_id, 'Intrus', 'x@exemple.fr')"
                ),
                {"tenant_id": seed.tenant_b},
            )


def test_requete_sans_contexte_tenant_echoue(app_engine: Engine, seed: SeedData) -> None:
    # current_setting est appelé sans missing_ok dans la policy : pas de
    # contexte tenant = erreur franche, pas un résultat vide silencieux.
    with app_engine.connect() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(SELECT_CUSTOMERS)


def test_pas_de_fuite_de_contexte_entre_requetes_du_pool(
    app_engine: Engine, seed: SeedData
) -> None:
    """Deux « requêtes » successives de tenants différents sur la même
    connexion physique (pool de taille 1) : le contexte posé par la première
    ne doit pas résider sur la connexion rendue au pool."""
    # Requête 1 : tenant A.
    with app_engine.connect() as conn:
        pid_1 = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        set_tenant(conn, seed.tenant_a)
        rows = conn.execute(SELECT_CUSTOMERS).fetchall()
        assert [row.id for row in rows] == [seed.customer_a]
        conn.commit()

    # Requête 2 : même connexion physique, réutilisée par le pool.
    with app_engine.connect() as conn:
        pid_2 = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        assert pid_2 == pid_1, "le pool devait réutiliser la même connexion"

        # Sans nouveau contexte : erreur, et surtout pas les données du
        # tenant A restées d'une transaction précédente.
        with pytest.raises(DBAPIError):
            conn.execute(SELECT_CUSTOMERS)
        conn.rollback()

        # Avec le contexte du tenant B : uniquement les données de B.
        set_tenant(conn, seed.tenant_b)
        rows = conn.execute(SELECT_CUSTOMERS).fetchall()
        assert [row.id for row in rows] == [seed.customer_b]


def test_users_isoles_par_tenant(app_engine: Engine, super_engine: Engine, seed: SeedData) -> None:
    # La table users est soumise au même régime RLS que les autres : un
    # tenant ne voit pas les comptes de l'autre.
    with super_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role) VALUES "
                "(gen_random_uuid(), :ta, 'ua@exemple.fr', 'hash', 'admin'), "
                "(gen_random_uuid(), :tb, 'ub@exemple.fr', 'hash', 'admin')"
            ),
            {"ta": seed.tenant_a, "tb": seed.tenant_b},
        )

    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        emails = conn.execute(text("SELECT email FROM users")).scalars().all()
    assert emails == ["ua@exemple.fr"]


def test_app_user_ne_peut_pas_desactiver_la_rls(app_engine: Engine, seed: SeedData) -> None:
    # Seul le propriétaire de la table (migrator) pourrait la désactiver ;
    # app_user doit être rejeté.
    with app_engine.connect() as conn:
        with pytest.raises(ProgrammingError):
            conn.execute(text("ALTER TABLE customers DISABLE ROW LEVEL SECURITY"))


def test_app_user_ne_peut_pas_contourner_via_row_security_off(
    app_engine: Engine, seed: SeedData
) -> None:
    # SET row_security = off n'est un contournement que pour un rôle
    # BYPASSRLS ; pour app_user, PostgreSQL refuse alors d'exécuter la
    # requête plutôt que d'ignorer la policy.
    with app_engine.connect() as conn:
        conn.execute(text("SET row_security = off"))
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(DBAPIError, match="row-level security"):
            conn.execute(SELECT_CUSTOMERS)
