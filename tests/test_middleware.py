"""Tests du middleware de contexte tenant, à travers l'API complète."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.conftest import SeedData


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_accessible_sans_tenant(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_requete_sans_en_tete_refusee(client: TestClient) -> None:
    response = client.get("/customers")
    assert response.status_code == 400


def test_en_tete_invalide_refuse(client: TestClient) -> None:
    response = client.get("/customers", headers={"X-Tenant-Id": "pas-un-uuid"})
    assert response.status_code == 400


def test_flag_desactive_ignore_l_en_tete(
    client: TestClient, seed: SeedData, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hors dev le flag est déjà rejeté au démarrage (cf. test_config) ; ici on
    # vérifie qu'une fois désactivé, l'en-tête n'est plus une porte d'entrée.
    monkeypatch.setattr(get_settings(), "tenant_header_enabled", False)
    response = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert response.status_code == 400


def test_liste_ne_voit_que_son_tenant(client: TestClient, seed: SeedData) -> None:
    response = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert response.status_code == 200
    assert [c["id"] for c in response.json()] == [str(seed.customer_a)]


def test_creation_portee_par_le_tenant_courant(client: TestClient, seed: SeedData) -> None:
    created = client.post(
        "/customers",
        headers={"X-Tenant-Id": str(seed.tenant_a)},
        json={"name": "Nouveau client", "email": "nouveau@exemple.fr"},
    )
    assert created.status_code == 201

    vus_par_a = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_a)})
    assert created.json()["id"] in [c["id"] for c in vus_par_a.json()]

    vus_par_b = client.get("/customers", headers={"X-Tenant-Id": str(seed.tenant_b)})
    assert created.json()["id"] not in [c["id"] for c in vus_par_b.json()]
