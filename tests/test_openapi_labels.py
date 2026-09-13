"""Libellés des opérations affichés dans /docs.

Sans summary explicite, FastAPI derive le libellé du nom de la fonction :
« Generate Cii Artifact » et « Generate Facturx Artifact », deux lignes
voisines quasi identiques dont on ne devine pas laquelle rend le XML et
laquelle rend le PDF.
"""

from fastapi.testclient import TestClient


def _schema(client: TestClient) -> dict:
    reponse = client.get("/openapi.json")
    assert reponse.status_code == 200
    return reponse.json()


def test_les_deux_generations_ont_un_libelle_francais_distinct(client: TestClient) -> None:
    chemins = _schema(client)["paths"]
    cii = chemins["/invoices/{invoice_id}/cii"]["post"]["summary"]
    facturx = chemins["/invoices/{invoice_id}/facturx"]["post"]["summary"]

    assert cii == "Génère le XML CII"
    assert facturx == "Assemble le PDF Factur-X"
    assert cii != facturx


def test_les_libelles_ne_sont_plus_ceux_derives_du_code(client: TestClient) -> None:
    chemins = _schema(client)["paths"]
    for chemin in ("/invoices/{invoice_id}/cii", "/invoices/{invoice_id}/facturx"):
        libelle = chemins[chemin]["post"]["summary"]
        assert "Artifact" not in libelle, chemin
        assert "Generate" not in libelle, chemin
