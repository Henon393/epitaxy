"""Tests 3b : chaînage cryptographique de l'audit (tamper-evidence)."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app
from tests.conftest import bearer, signup_tenant

CLIENT_JSON = {"name": "Client", "email": "client@exemple.fr"}


def _verify(client: TestClient, ctx: dict) -> dict:
    response = client.get("/audit/verify", headers=bearer(ctx["access_token"]))
    assert response.status_code == 200, response.text
    return response.json()


def _quelques_evenements(client: TestClient, ctx: dict, customers: int = 3) -> None:
    """Produit des entrées d'audit réelles : logins et créations."""
    client.post(
        "/auth/login",
        json={"tenant_id": ctx["tenant_id"], "email": ctx["email"], "password": ctx["password"]},
    )
    client.post(
        "/auth/login",
        json={"tenant_id": ctx["tenant_id"], "email": ctx["email"], "password": "mauvais-mdp!"},
    )
    for i in range(customers):
        assert (
            client.post(
                "/customers",
                headers=bearer(ctx["access_token"]),
                json={**CLIENT_JSON, "email": f"c{i}@exemple.fr"},
            ).status_code
            == 201
        )


def _audit_count(super_engine: Engine, tenant_id: str) -> int:
    with super_engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM audit_log WHERE tenant_id = :t"), {"t": tenant_id}
        ).scalar_one()


def test_chaine_reelle_integre(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _quelques_evenements(client, ctx)

    resultat = _verify(client, ctx)
    assert resultat["intact"] is True
    assert resultat["entries"] == _audit_count(super_engine, ctx["tenant_id"]) == 5
    assert resultat["broken_at_position"] is None

    # Les positions forment 1..n et chaque entrée est chaînée et versionnée.
    with super_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT position, prev_hash, entry_hash, hash_schema_version "
                "FROM audit_log WHERE tenant_id = :t ORDER BY position"
            ),
            {"t": ctx["tenant_id"]},
        ).fetchall()
    assert [r.position for r in rows] == [1, 2, 3, 4, 5]
    assert all(r.hash_schema_version == 1 for r in rows)
    # prev_hash de chaque entrée = entry_hash de la précédente.
    assert all(rows[i].prev_hash == rows[i - 1].entry_hash for i in range(1, len(rows)))


def test_alteration_detectee_au_bon_point(client: TestClient, super_engine: Engine) -> None:
    """Altérations par un rôle à pleins droits (superuser des fixtures,
    app_user ne pouvant pas) : détectées à la position exacte."""
    # Champ métier.
    ctx = signup_tenant(client)
    _quelques_evenements(client, ctx)
    with super_engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_log SET action = 'falsifie' WHERE tenant_id = :t AND position = 2"),
            {"t": ctx["tenant_id"]},
        )
    resultat = _verify(client, ctx)
    assert resultat["intact"] is False
    assert resultat["broken_at_position"] == 2
    assert "entry_hash" in resultat["reason"]

    # Métadonnées seules.
    ctx2 = signup_tenant(client, name="Tenant meta")
    _quelques_evenements(client, ctx2)
    with super_engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE audit_log SET metadata = \'{"ip": "10.0.0.99"}\'::jsonb '
                "WHERE tenant_id = :t AND position = 1"
            ),
            {"t": ctx2["tenant_id"]},
        )
    resultat2 = _verify(client, ctx2)
    assert (resultat2["intact"], resultat2["broken_at_position"]) == (False, 1)

    # Horodatage seul : il est couvert par le hash.
    ctx3 = signup_tenant(client, name="Tenant date")
    _quelques_evenements(client, ctx3)
    with super_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_log SET created_at = created_at + interval '1 hour' "
                "WHERE tenant_id = :t AND position = 3"
            ),
            {"t": ctx3["tenant_id"]},
        )
    resultat3 = _verify(client, ctx3)
    assert (resultat3["intact"], resultat3["broken_at_position"]) == (False, 3)


def test_suppression_mediane_detectee(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _quelques_evenements(client, ctx)
    with super_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM audit_log WHERE tenant_id = :t AND position = 2"),
            {"t": ctx["tenant_id"]},
        )
    resultat = _verify(client, ctx)
    assert resultat["intact"] is False
    assert resultat["broken_at_position"] == 2
    assert "contigu" in resultat["reason"]


def test_troncature_de_fin_signalee_par_la_tete(client: TestClient, super_engine: Engine) -> None:
    # Suppression de la DERNIÈRE entrée sans remise en arrière de la tête :
    # le croisement tête/chaîne la signale (limite documentée : tête et
    # chaîne remises en arrière ensemble = invisible, ancrage externe).
    ctx = signup_tenant(client)
    _quelques_evenements(client, ctx)
    with super_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit_log WHERE tenant_id = :t AND position = "
                "(SELECT max(position) FROM audit_log WHERE tenant_id = :t)"
            ),
            {"t": ctx["tenant_id"]},
        )
    resultat = _verify(client, ctx)
    assert resultat["intact"] is False
    assert "tête" in resultat["reason"] or "tete" in resultat["reason"]


def test_pas_de_fourche_sous_insertions_concurrentes(
    client: TestClient, super_engine: Engine
) -> None:
    ctx = signup_tenant(client)

    def creer(i: int):
        return TestClient(app).post(
            "/customers",
            headers=bearer(ctx["access_token"]),
            json={**CLIENT_JSON, "email": f"concurrent{i}@exemple.fr"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        reponses = list(pool.map(creer, range(12)))
    assert all(r.status_code == 201 for r in reponses)

    # Ordre total unique : positions 1..12, aucune fourche, chaîne intègre.
    resultat = _verify(client, ctx)
    assert resultat["intact"] is True
    assert resultat["entries"] == 12
    with super_engine.connect() as conn:
        positions = (
            conn.execute(
                text("SELECT position FROM audit_log WHERE tenant_id = :t ORDER BY position"),
                {"t": ctx["tenant_id"]},
            )
            .scalars()
            .all()
        )
    assert positions == list(range(1, 13))


def test_isolation_des_chaines_par_tenant(client: TestClient, super_engine: Engine) -> None:
    ctx_a = signup_tenant(client, name="Tenant A")
    ctx_b = signup_tenant(client, name="Tenant B")
    _quelques_evenements(client, ctx_a, customers=2)
    _quelques_evenements(client, ctx_b, customers=2)

    # Altération chez B uniquement.
    with super_engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_log SET action = 'falsifie' WHERE tenant_id = :t AND position = 1"),
            {"t": ctx_b["tenant_id"]},
        )

    resultat_a = _verify(client, ctx_a)
    resultat_b = _verify(client, ctx_b)
    assert resultat_a["intact"] is True
    assert resultat_a["entries"] == 4
    assert (resultat_b["intact"], resultat_b["broken_at_position"]) == (False, 1)


def test_rechainage_sans_la_cle_detecte(client: TestClient, super_engine: Engine) -> None:
    """Un attaquant à pleins droits SANS la clé altère une entrée puis
    re-chaîne toute la suite en SHA-256 nu, cohérente en apparence (prev_hash
    corrects, positions contiguës, tête alignée) : la vérification HMAC avec
    la clé détecte quand même, dès l'entrée altérée."""
    ctx = signup_tenant(client)
    _quelques_evenements(client, ctx)
    tenant_id = ctx["tenant_id"]

    with super_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, tenant_id, actor_id, action, target_type, target_id, metadata, "
                "created_at, position, prev_hash FROM audit_log WHERE tenant_id = :t "
                "ORDER BY position"
            ),
            {"t": tenant_id},
        ).fetchall()

        # Altère l'entrée 2 puis re-chaîne 2..n avec sha256 (sans clé), en
        # reproduisant fidèlement le format canonique v1.
        prev = conn.execute(
            text("SELECT entry_hash FROM audit_log WHERE tenant_id = :t AND position = 1"),
            {"t": tenant_id},
        ).scalar_one()
        for row in rows[1:]:
            action = "falsifie" if row.position == 2 else row.action
            metadata_canonical = (
                json.dumps(row.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if row.metadata is not None
                else ""
            )
            canonical = "\n".join(
                [
                    "1",
                    str(row.id),
                    str(row.position),
                    str(row.tenant_id),
                    str(row.actor_id) if row.actor_id is not None else "",
                    action,
                    row.target_type or "",
                    str(row.target_id) if row.target_id is not None else "",
                    metadata_canonical,
                    row.created_at.astimezone(UTC).isoformat(),
                ]
            )
            faux_hash = hashlib.sha256(
                canonical.encode("utf-8") + b"\n" + prev.encode("utf-8")
            ).hexdigest()
            conn.execute(
                text(
                    "UPDATE audit_log SET action = :action, prev_hash = :prev, "
                    "entry_hash = :h WHERE id = :id"
                ),
                {"action": action, "prev": prev, "h": faux_hash, "id": row.id},
            )
            prev = faux_hash
        conn.execute(
            text("UPDATE audit_chain_heads SET last_hash = :h WHERE tenant_id = :t"),
            {"h": prev, "t": tenant_id},
        )

    resultat = _verify(client, ctx)
    assert resultat["intact"] is False
    assert resultat["broken_at_position"] == 2
    assert "cl" in resultat["reason"] or "entry_hash" in resultat["reason"]


def test_verification_reservee_admin(client: TestClient) -> None:
    ctx = signup_tenant(client)
    creation = client.post(
        "/users",
        headers=bearer(ctx["access_token"]),
        json={"email": "compta@exemple.fr", "password": "mot-de-passe-solide", "role": "comptable"},
    )
    assert creation.status_code == 201
    login = client.post(
        "/auth/login",
        json={
            "tenant_id": ctx["tenant_id"],
            "email": "compta@exemple.fr",
            "password": "mot-de-passe-solide",
        },
    )
    token = login.json()["access_token"]
    assert client.get("/audit/verify", headers=bearer(token)).status_code == 403


def test_fail_closed_si_la_chaine_echoue(
    client: TestClient, super_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si le chaînage échoue, l'entrée d'audit échoue, donc la mutation
    métier aussi — le fail-closed de l'étape 3 est préservé."""
    import app.audit as audit_module

    ctx = signup_tenant(client)

    def chainage_saboteur(session, tenant_id):
        raise RuntimeError("tete de chaine indisponible")

    monkeypatch.setattr(audit_module, "next_chain_link", chainage_saboteur)
    sans_exception = TestClient(app, raise_server_exceptions=False)
    reponse = sans_exception.post(
        "/customers", headers=bearer(ctx["access_token"]), json=CLIENT_JSON
    )
    assert reponse.status_code == 500
    monkeypatch.undo()
    assert client.get("/customers", headers=bearer(ctx["access_token"])).json() == []
