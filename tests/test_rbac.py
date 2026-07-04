"""Tests RBAC : contrôle de rôle sur les routes protégées."""

from fastapi.testclient import TestClient

from tests.conftest import bearer, signup_tenant

CLIENT_JSON = {"name": "Client", "email": "client@exemple.fr"}


def _cree_et_connecte(client: TestClient, admin: dict, email: str, role: str) -> str:
    """Crée un utilisateur (via l'admin du tenant) puis le connecte ;
    retourne son access token."""
    creation = client.post(
        "/users",
        headers=bearer(admin["access_token"]),
        json={"email": email, "password": "mot-de-passe-solide", "role": role},
    )
    assert creation.status_code == 201, creation.text
    login = client.post(
        "/auth/login",
        json={"tenant_id": admin["tenant_id"], "email": email, "password": "mot-de-passe-solide"},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def test_lecture_seule_lit_mais_ne_cree_pas(client: TestClient) -> None:
    admin = signup_tenant(client)
    token = _cree_et_connecte(client, admin, "lecteur@exemple.fr", "lecture_seule")

    assert client.get("/customers", headers=bearer(token)).status_code == 200
    refus = client.post("/customers", headers=bearer(token), json=CLIENT_JSON)
    assert refus.status_code == 403


def test_comptable_cree_des_clients(client: TestClient) -> None:
    admin = signup_tenant(client)
    token = _cree_et_connecte(client, admin, "comptable@exemple.fr", "comptable")

    assert client.post("/customers", headers=bearer(token), json=CLIENT_JSON).status_code == 201


def test_admin_cree_des_clients(client: TestClient) -> None:
    admin = signup_tenant(client)
    ok = client.post("/customers", headers=bearer(admin["access_token"]), json=CLIENT_JSON)
    assert ok.status_code == 201


def test_creation_utilisateur_reservee_a_l_admin(client: TestClient) -> None:
    admin = signup_tenant(client)
    comptable = _cree_et_connecte(client, admin, "comptable@exemple.fr", "comptable")

    refus = client.post(
        "/users",
        headers=bearer(comptable),
        json={"email": "autre@exemple.fr", "password": "mot-de-passe-solide", "role": "comptable"},
    )
    assert refus.status_code == 403


def test_role_inconnu_refuse_a_la_validation(client: TestClient) -> None:
    admin = signup_tenant(client)
    refus = client.post(
        "/users",
        headers=bearer(admin["access_token"]),
        json={"email": "x@exemple.fr", "password": "mot-de-passe-solide", "role": "superadmin"},
    )
    assert refus.status_code == 422


def test_email_duplique_dans_le_tenant_409(client: TestClient) -> None:
    admin = signup_tenant(client)
    corps = {"email": "double@exemple.fr", "password": "mot-de-passe-solide", "role": "comptable"}
    assert (
        client.post("/users", headers=bearer(admin["access_token"]), json=corps).status_code == 201
    )
    assert (
        client.post("/users", headers=bearer(admin["access_token"]), json=corps).status_code == 409
    )


def test_meme_email_possible_dans_deux_tenants(client: TestClient) -> None:
    # L'unicité de l'email est PAR tenant : deux organisations peuvent
    # employer la même adresse.
    a = signup_tenant(client, name="Tenant A")
    b = signup_tenant(client, name="Tenant B")
    corps = {"email": "partage@exemple.fr", "password": "mot-de-passe-solide", "role": "comptable"}
    assert client.post("/users", headers=bearer(a["access_token"]), json=corps).status_code == 201
    assert client.post("/users", headers=bearer(b["access_token"]), json=corps).status_code == 201
