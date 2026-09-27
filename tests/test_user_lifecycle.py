"""Cycle de vie des comptes : désactivation, changement de rôle, révocation.

Le point central de ces tests est la coupure IMMÉDIATE : un jeton d'accès
déjà émis doit être refusé dès la désactivation ou la rétrogradation, et
non à son expiration. Sans cela, un compte compromis conserverait ses
droits le temps de vie du jeton.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.conftest import bearer, signup_tenant


def _audit_actions(super_engine: Engine, tenant_id: str, prefixe: str) -> list:
    with super_engine.connect() as conn:
        return [
            r
            for r in conn.execute(
                text(
                    "SELECT actor_id, action, target_type, target_id, metadata "
                    "FROM audit_log WHERE tenant_id = :t ORDER BY position"
                ),
                {"t": str(tenant_id)},
            ).fetchall()
            if r.action.startswith(prefixe)
        ]


def _cree_compte(client: TestClient, admin: dict, role: str, email: str) -> dict:
    reponse = client.post(
        "/users",
        headers=bearer(admin["access_token"]),
        json={"email": email, "password": "mot-de-passe-solide", "role": role},
    )
    assert reponse.status_code == 201, reponse.text
    return reponse.json()


def _connexion(client: TestClient, tenant_id: str, email: str) -> dict:
    reponse = client.post(
        "/auth/login",
        json={"tenant_id": tenant_id, "email": email, "password": "mot-de-passe-solide"},
    )
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


# --- Désactivation ---------------------------------------------------------


def test_desactivation_coupe_le_jeton_d_acces_immediatement(client: TestClient) -> None:
    """Le point clé : la coupure est immédiate, pas à l'expiration du jeton."""
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")
    jetons = _connexion(client, admin["tenant_id"], "cible@exemple.fr")

    # Le jeton fonctionne avant la désactivation.
    assert client.get("/invoices", headers=bearer(jetons["access_token"])).status_code == 200

    desactive = client.patch(
        f"/users/{cible['id']}",
        headers=bearer(admin["access_token"]),
        json={"is_active": False},
    )
    assert desactive.status_code == 200
    assert desactive.json()["is_active"] is False

    # Le MÊME jeton, non expiré, est refusé dès maintenant.
    refus = client.get("/invoices", headers=bearer(jetons["access_token"]))
    assert refus.status_code == 401
    assert refus.json()["detail"] == "Session révoquée, reconnectez-vous."


def test_desactivation_coupe_le_rafraichissement(client: TestClient) -> None:
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")
    jetons = _connexion(client, admin["tenant_id"], "cible@exemple.fr")

    client.patch(
        f"/users/{cible['id']}",
        headers=bearer(admin["access_token"]),
        json={"is_active": False},
    )
    refresh = client.post("/auth/refresh", json={"refresh_token": jetons["refresh_token"]})
    assert refresh.status_code == 401


def test_desactivation_empeche_toute_reconnexion(client: TestClient) -> None:
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")

    client.patch(
        f"/users/{cible['id']}",
        headers=bearer(admin["access_token"]),
        json={"is_active": False},
    )
    refus = client.post(
        "/auth/login",
        json={
            "tenant_id": admin["tenant_id"],
            "email": "cible@exemple.fr",
            "password": "mot-de-passe-solide",
        },
    )
    assert refus.status_code == 401


def test_reactivation_rend_le_compte_utilisable(client: TestClient) -> None:
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")
    entetes = bearer(admin["access_token"])

    client.patch(f"/users/{cible['id']}", headers=entetes, json={"is_active": False})
    reactive = client.patch(f"/users/{cible['id']}", headers=entetes, json={"is_active": True})
    assert reactive.status_code == 200
    assert reactive.json()["is_active"] is True

    # Une nouvelle session s'ouvre, avec une version de session à jour.
    jetons = _connexion(client, admin["tenant_id"], "cible@exemple.fr")
    assert client.get("/invoices", headers=bearer(jetons["access_token"])).status_code == 200


# --- Changement de rôle ----------------------------------------------------


def test_retrogradation_coupe_les_droits_immediatement(client: TestClient) -> None:
    """Le rôle vient du claim du jeton : sans révocation, un admin
    rétrogradé resterait admin jusqu'à expiration."""
    admin = signup_tenant(client)
    second = _cree_compte(client, admin, "admin", "second@exemple.fr")
    jetons = _connexion(client, admin["tenant_id"], "second@exemple.fr")
    entetes_second = bearer(jetons["access_token"])

    # Il exerce bien ses droits d'administrateur.
    assert client.get("/audit/verify", headers=entetes_second).status_code == 200

    retrograde = client.patch(
        f"/users/{second['id']}",
        headers=bearer(admin["access_token"]),
        json={"role": "lecture_seule"},
    )
    assert retrograde.status_code == 200
    assert retrograde.json()["role"] == "lecture_seule"

    # Son jeton portait role=admin : il doit être refusé, pas honoré.
    assert client.get("/audit/verify", headers=entetes_second).status_code == 401

    # Après reconnexion, le nouveau rôle s'applique : 403 et non plus 401.
    nouveaux = _connexion(client, admin["tenant_id"], "second@exemple.fr")
    assert client.get("/audit/verify", headers=bearer(nouveaux["access_token"])).status_code == 403


def test_promotion_donne_les_nouveaux_droits(client: TestClient) -> None:
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "lecture_seule", "cible@exemple.fr")

    promu = client.patch(
        f"/users/{cible['id']}",
        headers=bearer(admin["access_token"]),
        json={"role": "comptable"},
    )
    assert promu.status_code == 200
    assert promu.json()["role"] == "comptable"

    jetons = _connexion(client, admin["tenant_id"], "cible@exemple.fr")
    cree = client.post(
        "/customers",
        headers=bearer(jetons["access_token"]),
        json={"name": "Client", "email": "client@exemple.fr"},
    )
    assert cree.status_code == 201


# --- Garde-fou du dernier administrateur -----------------------------------


def test_le_dernier_admin_ne_peut_pas_se_desactiver(client: TestClient) -> None:
    admin = signup_tenant(client)
    refus = client.patch(
        f"/users/{admin['user_id']}",
        headers=bearer(admin["access_token"]),
        json={"is_active": False},
    )
    assert refus.status_code == 409
    assert "dernier administrateur" in refus.json()["detail"].lower()
    # État inchangé : il travaille toujours.
    assert client.get("/invoices", headers=bearer(admin["access_token"])).status_code == 200


def test_le_dernier_admin_ne_peut_pas_se_retrograder(client: TestClient) -> None:
    admin = signup_tenant(client)
    refus = client.patch(
        f"/users/{admin['user_id']}",
        headers=bearer(admin["access_token"]),
        json={"role": "comptable"},
    )
    assert refus.status_code == 409
    assert client.get("/audit/verify", headers=bearer(admin["access_token"])).status_code == 200


def test_desactiver_le_dernier_autre_admin_refuse(client: TestClient) -> None:
    """Un admin déjà inactif ne compte pas : il ne peut pas couvrir le départ
    du dernier administrateur actif."""
    admin = signup_tenant(client)
    entetes = bearer(admin["access_token"])
    dormant = _cree_compte(client, admin, "admin", "dormant@exemple.fr")
    client.patch(f"/users/{dormant['id']}", headers=entetes, json={"is_active": False})

    refus = client.patch(f"/users/{admin['user_id']}", headers=entetes, json={"is_active": False})
    assert refus.status_code == 409


def test_un_admin_peut_se_desactiver_si_un_autre_reste(client: TestClient) -> None:
    admin = signup_tenant(client)
    entetes = bearer(admin["access_token"])
    _cree_compte(client, admin, "admin", "releve@exemple.fr")

    depart = client.patch(f"/users/{admin['user_id']}", headers=entetes, json={"is_active": False})
    assert depart.status_code == 200
    # Sa propre session est coupée dans la foulée.
    assert client.get("/invoices", headers=entetes).status_code == 401


# --- Traçabilité -----------------------------------------------------------


def test_changements_traces_avec_acteur_et_cible(client: TestClient, super_engine: Engine) -> None:
    admin = signup_tenant(client)
    entetes = bearer(admin["access_token"])
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")

    client.patch(f"/users/{cible['id']}", headers=entetes, json={"role": "lecture_seule"})
    client.patch(f"/users/{cible['id']}", headers=entetes, json={"is_active": False})
    client.patch(f"/users/{cible['id']}", headers=entetes, json={"is_active": True})

    rows = [
        r
        for r in _audit_actions(super_engine, admin["tenant_id"], "user_")
        if r.action != "user_created"
    ]
    assert [(r.action, r.target_type, str(r.target_id), r.metadata) for r in rows] == [
        ("user_role_changed", "user", cible["id"], {"from": "comptable", "to": "lecture_seule"}),
        ("user_deactivated", "user", cible["id"], {"role": "lecture_seule"}),
        ("user_reactivated", "user", cible["id"], {"role": "lecture_seule"}),
    ]
    # L'acteur est l'administrateur qui agit, jamais le compte visé.
    assert all(str(r.actor_id) == admin["user_id"] for r in rows)


def test_un_patch_qui_change_tout_ecrit_deux_entrees(
    client: TestClient, super_engine: Engine
) -> None:
    admin = signup_tenant(client)
    entetes = bearer(admin["access_token"])
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")

    reponse = client.patch(
        f"/users/{cible['id']}",
        headers=entetes,
        json={"role": "lecture_seule", "is_active": False},
    )
    assert reponse.status_code == 200

    actions = [
        r.action
        for r in _audit_actions(super_engine, admin["tenant_id"], "user_")
        if r.action != "user_created"
    ]
    assert actions == ["user_role_changed", "user_deactivated"]


def test_patch_sans_effet_ne_trace_rien(client: TestClient, super_engine: Engine) -> None:
    admin = signup_tenant(client)
    entetes = bearer(admin["access_token"])
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")

    reponse = client.patch(f"/users/{cible['id']}", headers=entetes, json={"role": "comptable"})
    assert reponse.status_code == 200
    assert [r.action for r in _audit_actions(super_engine, admin["tenant_id"], "user_")] == [
        "user_created",
        "user_created",
    ]


# --- Contrôle d'accès et validation ----------------------------------------


def test_patch_reserve_a_l_admin(client: TestClient) -> None:
    admin = signup_tenant(client)
    cible = _cree_compte(client, admin, "comptable", "cible@exemple.fr")
    _cree_compte(client, admin, "lecture_seule", "lecteur@exemple.fr")

    for email in ("cible@exemple.fr", "lecteur@exemple.fr"):
        jetons = _connexion(client, admin["tenant_id"], email)
        refus = client.patch(
            f"/users/{cible['id']}",
            headers=bearer(jetons["access_token"]),
            json={"role": "admin"},
        )
        assert refus.status_code == 403, email


def test_patch_sur_un_compte_inexistant(client: TestClient) -> None:
    admin = signup_tenant(client)
    refus = client.patch(
        f"/users/{uuid.uuid4()}",
        headers=bearer(admin["access_token"]),
        json={"role": "comptable"},
    )
    assert refus.status_code == 404


def test_patch_vide_refuse(client: TestClient) -> None:
    admin = signup_tenant(client)
    refus = client.patch(
        f"/users/{admin['user_id']}", headers=bearer(admin["access_token"]), json={}
    )
    assert refus.status_code == 422


def test_un_tenant_ne_peut_pas_modifier_le_compte_d_un_autre(client: TestClient) -> None:
    a = signup_tenant(client, name="Tenant A", email="a@exemple.fr")
    b = signup_tenant(client, name="Tenant B", email="b@exemple.fr")

    refus = client.patch(
        f"/users/{b['user_id']}",
        headers=bearer(a["access_token"]),
        json={"is_active": False},
    )
    assert refus.status_code == 404
    # La cible est intacte chez elle.
    assert client.get("/invoices", headers=bearer(b["access_token"])).status_code == 200
