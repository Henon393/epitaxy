"""Le schéma de sécurité servi à /docs : bouton Authorize.

Purement documentaire : ces tests vérifient que l'OpenAPI décrit fidèlement
le Bearer que le middleware exige déjà, et que ce marquage n'ouvre rien.
"""

from fastapi.testclient import TestClient

from app.main import BEARER_SCHEME_NAME
from app.middleware import EXEMPT_PATHS

_METHODES = {"get", "put", "post", "delete", "patch"}


def _schema(client: TestClient) -> dict:
    reponse = client.get("/openapi.json")
    assert reponse.status_code == 200
    return reponse.json()


def _operations(schema: dict, path: str) -> list[dict]:
    return [op for methode, op in schema["paths"][path].items() if methode in _METHODES]


def test_le_schema_de_securite_bearer_est_declare(client: TestClient) -> None:
    """Sans securityScheme, Swagger UI n'affiche aucun bouton Authorize."""
    scheme = _schema(client)["components"]["securitySchemes"][BEARER_SCHEME_NAME]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"


def test_les_routes_protegees_referencent_le_schema(client: TestClient) -> None:
    schema = _schema(client)
    for path in ("/invoices", "/customers", "/company-profile", "/audit/verify"):
        for operation in _operations(schema, path):
            assert operation["security"] == [{BEARER_SCHEME_NAME: []}], path


def test_les_routes_exemptees_restent_sans_securite(client: TestClient) -> None:
    """signup/login/refresh doivent rester appelables sans jeton depuis /docs."""
    schema = _schema(client)
    for path in ("/health", "/auth/signup", "/auth/login", "/auth/refresh"):
        for operation in _operations(schema, path):
            assert "security" not in operation, path


def test_le_marquage_suit_exactement_les_chemins_exemptes(client: TestClient) -> None:
    """EXEMPT_PATHS reste l'unique source de vérité : toute route non exemptée
    est marquée, aucune route exemptée ne l'est."""
    schema = _schema(client)
    for path, operations in schema["paths"].items():
        attendu = path not in EXEMPT_PATHS
        for methode, operation in operations.items():
            if methode in _METHODES:
                assert ("security" in operation) is attendu, f"{methode.upper()} {path}"


def test_la_documentation_n_ouvre_aucune_route(client: TestClient) -> None:
    """Garde-fou : le schéma est déclaratif, le middleware reste seul juge."""
    assert client.get("/invoices").status_code == 401
    assert client.get("/invoices", headers={"Authorization": "Bearer faux"}).status_code == 401


def test_la_description_explique_comment_s_authentifier(client: TestClient) -> None:
    description = _schema(client)["info"]["description"]
    assert "Authorize" in description
    assert "access_token" in description
