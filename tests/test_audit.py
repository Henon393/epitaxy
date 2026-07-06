"""Tests du journal d'audit : inaltérabilité, câblage contexte, isolation."""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

import app.routers.customers as customers_module
from app.audit import record
from app.db import (
    reset_current_tenant,
    session_for_tenant,
    set_current_tenant,
)
from app.identity import CurrentUser, reset_current_user, set_current_user
from app.models import AuditAction
from tests.conftest import SeedData, bearer, set_tenant, signup_tenant


def _audit_rows(super_engine: Engine, tenant_id: str | uuid.UUID) -> list:
    with super_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT actor_id, action, target_type, target_id, metadata "
                "FROM audit_log WHERE tenant_id = :t ORDER BY created_at"
            ),
            {"t": str(tenant_id)},
        ).fetchall()


def _seed_audit_row(super_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    # Ligne brute pour les tests d'inaltérabilité/isolation : les colonnes
    # de chaîne (3b) sont remplies avec des valeurs factices, la validité
    # de la chaîne n'est pas l'objet de ces tests.
    row_id = uuid.uuid4()
    with super_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (id, tenant_id, action, position, prev_hash, "
                "entry_hash, hash_schema_version) "
                "SELECT :id, :t, 'login_failed', COALESCE(max(position), 0) + 1, "
                "repeat('0', 64), repeat('0', 64), 1 "
                "FROM audit_log WHERE tenant_id = :t"
            ),
            {"id": row_id, "t": tenant_id},
        )
    return row_id


# --- Inaltérabilité : UPDATE, DELETE, TRUNCATE refusés à app_user ----------


def test_update_d_une_ligne_d_audit_refuse(
    app_engine: Engine, super_engine: Engine, seed: SeedData
) -> None:
    row_id = _seed_audit_row(super_engine, seed.tenant_a)
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(
                text("UPDATE audit_log SET action = 'falsifie' WHERE id = :id"), {"id": row_id}
            )


def test_delete_d_une_ligne_d_audit_refuse(
    app_engine: Engine, super_engine: Engine, seed: SeedData
) -> None:
    row_id = _seed_audit_row(super_engine, seed.tenant_a)
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})


def test_truncate_de_l_audit_refuse(app_engine: Engine, seed: SeedData) -> None:
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("TRUNCATE audit_log"))


# --- Câblage : les événements portent l'acteur et le tenant du contexte ----


def test_login_reussi_et_echoue_traces(client: TestClient, super_engine: Engine) -> None:
    s = signup_tenant(client)

    ok = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": s["email"], "password": s["password"]},
    )
    assert ok.status_code == 200
    ko = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": s["email"], "password": "mauvais-mdp!"},
    )
    assert ko.status_code == 401
    inconnu = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": "inconnu@exemple.fr", "password": "x" * 12},
    )
    assert inconnu.status_code == 401

    rows = _audit_rows(super_engine, s["tenant_id"])
    actions = [row.action for row in rows]
    assert actions == ["login_succeeded", "login_failed", "login_failed"]
    # Le succès et le mauvais mot de passe portent l'acteur ; l'email
    # inconnu n'a pas d'acteur.
    assert str(rows[0].actor_id) == s["user_id"]
    assert str(rows[1].actor_id) == s["user_id"]
    assert rows[2].actor_id is None


def test_creation_et_suppression_de_customer_tracees(
    client: TestClient, super_engine: Engine
) -> None:
    s = signup_tenant(client)
    cree = client.post(
        "/customers",
        headers=bearer(s["access_token"]),
        json={"name": "Client", "email": "client@exemple.fr"},
    )
    assert cree.status_code == 201
    customer_id = cree.json()["id"]

    supprime = client.delete(f"/customers/{customer_id}", headers=bearer(s["access_token"]))
    assert supprime.status_code == 204

    rows = [r for r in _audit_rows(super_engine, s["tenant_id"]) if r.action.startswith("customer")]
    assert [(r.action, r.target_type, str(r.target_id)) for r in rows] == [
        ("customer_created", "customer", customer_id),
        ("customer_deleted", "customer", customer_id),
    ]
    # L'acteur vient du ContextVar d'identité posé par le middleware.
    assert all(str(r.actor_id) == s["user_id"] for r in rows)


def test_le_service_lit_le_contexte_courant(super_engine: Engine, seed: SeedData) -> None:
    acteur = uuid.uuid4()
    tenant_token = set_current_tenant(seed.tenant_a)
    user_token = set_current_user(CurrentUser(id=acteur, tenant_id=seed.tenant_a, role="admin"))
    try:
        with session_for_tenant(seed.tenant_a) as session:
            record(session, AuditAction.customer_created, metadata={"via": "test"})
    finally:
        reset_current_user(user_token)
        reset_current_tenant(tenant_token)

    rows = _audit_rows(super_engine, seed.tenant_a)
    assert len(rows) == 1
    assert rows[0].actor_id == acteur
    assert rows[0].metadata == {"via": "test"}


# --- Fail-closed : audit en échec = mutation annulée ------------------------


def test_echec_d_audit_annule_la_mutation_metier(
    client: TestClient, super_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la ligne d'audit ne peut pas s'écrire, le customer ne doit pas
    être créé : même transaction, fail-closed."""
    s = signup_tenant(client)

    def record_saboteur(session, action, **kwargs):
        # Force un tenant_id étranger : l'INSERT d'audit viole le WITH CHECK
        # de la policy RLS et échoue au flush.
        kwargs["tenant_id"] = uuid.uuid4()
        record(session, action, **kwargs)

    monkeypatch.setattr(customers_module, "record", record_saboteur)

    from app.main import app

    sans_exception = TestClient(app, raise_server_exceptions=False)
    response = sans_exception.post(
        "/customers",
        headers=bearer(s["access_token"]),
        json={"name": "Fantome", "email": "fantome@exemple.fr"},
    )
    assert response.status_code == 500

    monkeypatch.undo()
    liste = client.get("/customers", headers=bearer(s["access_token"]))
    assert liste.json() == [], "la mutation métier aurait dû être annulée avec l'audit"


# --- Isolation tenant sur audit_log -----------------------------------------


def test_un_tenant_ne_lit_que_ses_entrees_d_audit(
    app_engine: Engine, super_engine: Engine, seed: SeedData
) -> None:
    _seed_audit_row(super_engine, seed.tenant_a)
    _seed_audit_row(super_engine, seed.tenant_b)

    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        rows = conn.execute(text("SELECT tenant_id FROM audit_log")).fetchall()
    assert {row.tenant_id for row in rows} == {seed.tenant_a}


def test_insert_d_audit_pour_un_autre_tenant_rejete(app_engine: Engine, seed: SeedData) -> None:
    with app_engine.connect() as conn:
        set_tenant(conn, seed.tenant_a)
        with pytest.raises(DBAPIError, match="row-level security"):
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, tenant_id, action) "
                    "VALUES (gen_random_uuid(), :t, 'login_failed')"
                ),
                {"t": seed.tenant_b},
            )


# --- Événements sans tenant : log applicatif, jamais la table ---------------


def test_rate_limit_part_dans_le_log_applicatif_pas_en_base(
    client: TestClient, super_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    s = signup_tenant(client)
    corps = {"tenant_id": s["tenant_id"], "email": s["email"], "password": "mauvais-mdp!"}
    for _ in range(5):
        client.post("/auth/login", json=corps)
    avant = len(_audit_rows(super_engine, s["tenant_id"]))

    with caplog.at_level(logging.WARNING, logger="app.security"):
        reponse = client.post("/auth/login", json=corps)
    assert reponse.status_code == 429
    assert "login_rate_limited" in caplog.text

    # La tentative bloquée n'a produit aucune ligne en base.
    assert len(_audit_rows(super_engine, s["tenant_id"])) == avant


def test_login_sur_tenant_inexistant_part_dans_le_log_applicatif(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.security"):
        response = client.post(
            "/auth/login",
            json={
                "tenant_id": str(uuid.uuid4()),
                "email": "quelquun@exemple.fr",
                "password": "x" * 12,
            },
        )
    assert response.status_code == 401
    assert "login_failed" in caplog.text
