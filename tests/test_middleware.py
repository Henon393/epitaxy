"""Tests du middleware : exigence d'authentification et repli dev X-Tenant-Id."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import SeedData


def test_health_accessible_sans_auth(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_requete_sans_auth_refusee(client: TestClient) -> None:
    response = client.get("/customers")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_en_tete_tenant_invalide_refuse(client: TestClient) -> None:
    assert client.get("/customers", headers={"X-Tenant-Id": "pas-un-uuid"}).status_code == 401


def test_flag_desactive_ignore_l_en_tete(
    client: TestClient, seed: SeedData, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hors dev le flag est déjà rejeté au démarrage (cf. test_config) ; ici on
    # vérifie qu'une fois désactivé, l'en-tête seul ne donne plus accès.
    monkeypatch.setattr(get_settings(), "tenant_header_enabled", False)
    response = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert response.status_code == 401


def test_en_tete_dev_donne_le_contexte_tenant_en_lecture(
    client: TestClient, seed: SeedData
) -> None:
    response = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert response.status_code == 200
    assert [c["id"] for c in response.json()] == [str(seed.customer_a)]


def test_en_tete_dev_sans_identite_ne_passe_pas_le_rbac(client: TestClient, seed: SeedData) -> None:
    # Le repli dev pose un tenant mais aucune identité : les routes exigeant
    # un rôle restent fermées par ce chemin.
    response = client.post(
        "/customers",
        headers={"X-Tenant-Id": str(seed.tenant_a)},
        json={"name": "Client", "email": "client@exemple.fr"},
    )
    assert response.status_code == 401

    assert client.get("/me", headers={"X-Tenant-Id": str(seed.tenant_a)}).status_code == 401
