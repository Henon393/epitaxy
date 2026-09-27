"""Tests du middleware : le JWT vérifié est la seule voie d'entrée."""

from fastapi.testclient import TestClient

from tests.conftest import SeedData


def test_health_accessible_sans_auth(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_requete_sans_auth_refusee(client: TestClient) -> None:
    response = client.get("/customers")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_en_tete_tenant_ne_donne_aucun_acces(client: TestClient, seed: SeedData) -> None:
    """Connaître l'identifiant d'un tenant ne donne accès à rien.

    Un mécanisme de résolution par en-tête a existé pour le développement.
    Il posait un contexte tenant sans authentification et exposait en
    lecture les factures, leurs artefacts, les clients et le profil
    vendeur. Il a été supprimé ; ce test verrouille sa disparition.
    """
    entetes = {"X-Tenant-Id": str(seed.tenant_a)}
    for chemin in (
        "/customers",
        "/invoices",
        "/company-profile",
        "/audit/verify",
        "/me",
    ):
        reponse = client.get(chemin, headers=entetes)
        assert reponse.status_code == 401, chemin
        assert reponse.json()["detail"] == "Authentification requise.", chemin

    # Aucune écriture non plus, par ce chemin ni par un autre.
    creation = client.post(
        "/customers",
        headers=entetes,
        json={"name": "Client", "email": "client@exemple.fr"},
    )
    assert creation.status_code == 401
