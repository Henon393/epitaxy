"""Tests d'authentification : tokens, anti-énumération, refresh, rate limit."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import app.routers.auth as auth_module
from app.config import get_settings
from app.security import DUMMY_HASH
from tests.conftest import SeedData, bearer, signup_tenant


def _decode_sans_verif(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


def test_signup_puis_login_puis_acces(client: TestClient) -> None:
    s = signup_tenant(client)

    login = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": s["email"], "password": s["password"]},
    )
    assert login.status_code == 200
    access = login.json()["access_token"]

    response = client.get("/customers", headers=bearer(access))
    assert response.status_code == 200
    assert response.json() == []


def test_me_reflete_le_token(client: TestClient) -> None:
    s = signup_tenant(client)
    me = client.get("/me", headers=bearer(s["access_token"]))
    assert me.status_code == 200
    assert me.json() == {
        "user_id": s["user_id"],
        "tenant_id": s["tenant_id"],
        "role": "admin",
    }


def test_claim_tenant_altere_rejete_par_la_signature(client: TestClient) -> None:
    s = signup_tenant(client)
    claims = _decode_sans_verif(s["access_token"])
    claims["tenant_id"] = str(uuid.uuid4())
    # L'attaquant ne connaît pas la clé : il re-signe avec la sienne.
    forge = pyjwt.encode(claims, "cle-de-l-attaquant", algorithm="HS256")
    response = client.get("/customers", headers=bearer(forge))
    assert response.status_code == 401


def test_alg_none_rejete(client: TestClient) -> None:
    s = signup_tenant(client)
    claims = _decode_sans_verif(s["access_token"])
    claims["tenant_id"] = str(uuid.uuid4())
    forge = pyjwt.encode(claims, None, algorithm="none")
    assert client.get("/customers", headers=bearer(forge)).status_code == 401


def test_hs512_meme_avec_la_bonne_cle_rejete(client: TestClient) -> None:
    # algorithms=["HS256"] est imposé au décodage : l'alg du header n'est
    # jamais suivi, même signé avec la vraie clé.
    s = signup_tenant(client)
    claims = _decode_sans_verif(s["access_token"])
    forge = pyjwt.encode(claims, get_settings().jwt_secret, algorithm="HS512")
    assert client.get("/customers", headers=bearer(forge)).status_code == 401


def test_token_expire_rejete(client: TestClient) -> None:
    s = signup_tenant(client)
    claims = _decode_sans_verif(s["access_token"])
    now = datetime.now(UTC)
    claims["iat"] = now - timedelta(hours=1)
    claims["exp"] = now - timedelta(minutes=1)
    expire = pyjwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    assert client.get("/customers", headers=bearer(expire)).status_code == 401


def test_token_malforme_rejete(client: TestClient) -> None:
    assert client.get("/customers", headers=bearer("pas.un.jwt")).status_code == 401
    assert client.get("/customers", headers={"Authorization": "Basic abc"}).status_code == 401


def test_meme_401_pour_inconnu_et_mauvais_mot_de_passe(client: TestClient) -> None:
    s = signup_tenant(client)
    inconnu = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": "inconnu@exemple.fr", "password": "x" * 12},
    )
    mauvais = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": s["email"], "password": "mauvais-mdp!"},
    )
    assert inconnu.status_code == mauvais.status_code == 401
    assert inconnu.json() == mauvais.json()


def test_utilisateur_inexistant_paie_le_cout_argon2(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-énumération par timing : le chemin « inexistant » doit déclencher
    exactement une vérification argon2, contre le hash factice constant."""
    s = signup_tenant(client)
    verifications: list[str] = []
    reelle = auth_module.verify_password

    def espion(password_hash: str, password: str) -> bool:
        verifications.append(password_hash)
        return reelle(password_hash, password)

    monkeypatch.setattr(auth_module, "verify_password", espion)
    response = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": "inconnu@exemple.fr", "password": "x" * 12},
    )
    assert response.status_code == 401
    assert verifications == [DUMMY_HASH]


def test_rotation_et_revocation_de_la_famille_de_refresh(client: TestClient) -> None:
    s = signup_tenant(client)
    r1 = s["refresh_token"]

    # Rotation normale : R1 consommé, R2 émis dans la même famille.
    rotation = client.post("/auth/refresh", json={"refresh_token": r1})
    assert rotation.status_code == 200
    r2 = rotation.json()["refresh_token"]
    assert _decode_sans_verif(r2)["family_id"] == _decode_sans_verif(r1)["family_id"]

    # Réutilisation de R1 (déjà consommé) : vol présumé, famille révoquée.
    assert client.post("/auth/refresh", json={"refresh_token": r1}).status_code == 401

    # R2, pourtant jamais utilisé, meurt avec sa famille.
    assert client.post("/auth/refresh", json={"refresh_token": r2}).status_code == 401

    # Un nouveau login ouvre une nouvelle famille, qui fonctionne.
    login = client.post(
        "/auth/login",
        json={"tenant_id": s["tenant_id"], "email": s["email"], "password": s["password"]},
    )
    encore = client.post("/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert encore.status_code == 200


def test_confusion_access_refresh_rejetee(client: TestClient) -> None:
    s = signup_tenant(client)
    # Un access token n'est pas un refresh...
    assert (
        client.post("/auth/refresh", json={"refresh_token": s["access_token"]}).status_code == 401
    )
    # ...et un refresh n'ouvre pas l'API.
    assert client.get("/customers", headers=bearer(s["refresh_token"])).status_code == 401


def test_rate_limit_sur_le_login(client: TestClient) -> None:
    s = signup_tenant(client)
    corps = {"tenant_id": s["tenant_id"], "email": s["email"], "password": "mauvais-mdp!"}
    limite = get_settings().login_rate_limit_attempts
    for _ in range(limite):
        assert client.post("/auth/login", json=corps).status_code == 401
    assert client.post("/auth/login", json=corps).status_code == 429
    # Même le bon mot de passe est bloqué tant que la fenêtre court.
    corps["password"] = s["password"]
    assert client.post("/auth/login", json=corps).status_code == 429


def test_jwt_prioritaire_sur_l_en_tete_dev(client: TestClient, seed: SeedData) -> None:
    # Un JWT du tenant fraîchement créé + X-Tenant-Id pointant sur un tenant
    # seedé : l'en-tête ne doit avoir aucun effet.
    s = signup_tenant(client)
    response = client.get(
        "/customers",
        headers={**bearer(s["access_token"]), "X-Tenant-Id": str(seed.tenant_a)},
    )
    assert response.status_code == 200
    assert response.json() == []
