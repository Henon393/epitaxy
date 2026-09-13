from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from starlette.responses import JSONResponse

from app.db import MissingTenantContextError
from app.middleware import EXEMPT_PATHS, TenantContextMiddleware
from app.routers import (
    audit,
    auth,
    company_profile,
    customers,
    invoices,
    mfa,
    transmissions,
    users,
)

API_TITLE = "Facturation — Solution Compatible (apprentissage)"

API_DESCRIPTION = """\
API de facturation électronique multi-tenant (Solution Compatible adossée à
une Plateforme Agréée, mockée à ce stade).

### S'authentifier depuis cette page

1. Appeler `POST /auth/signup` (ou `POST /auth/login`) — ces routes sont
   ouvertes, elles ne demandent pas de jeton.
2. Copier la valeur de `access_token` renvoyée dans la réponse.
3. Cliquer sur **Authorize** en haut à droite, coller le jeton, valider.

Toutes les autres routes sont alors appelables : le jeton part dans l'en-tête
`Authorization: Bearer <access_token>`, d'où le middleware déduit le tenant et
l'utilisateur. Sans jeton valide, la réponse est `401` avant l'exécution de la
route.

Le jeton d'accès est de courte durée : en cas de `401` après un moment,
rejouer `POST /auth/refresh` avec le `refresh_token`, puis re-cliquer sur
**Authorize** avec le nouvel `access_token`.
"""

# Nom du schéma de sécurité tel qu'il apparaît dans l'OpenAPI et sur le
# bouton Authorize de /docs.
BEARER_SCHEME_NAME = "BearerJWT"

BEARER_SCHEME: dict[str, Any] = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": (
        "Jeton d'accès renvoyé par /auth/signup, /auth/login ou /auth/refresh "
        "(champ access_token). Coller la valeur brute, sans le préfixe « Bearer »."
    ),
}

_OPERATION_KEYS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}

app = FastAPI(title=API_TITLE, description=API_DESCRIPTION)
app.add_middleware(TenantContextMiddleware)

app.include_router(auth.router)
app.include_router(mfa.router)
app.include_router(users.router)
app.include_router(customers.router)
app.include_router(company_profile.router)
app.include_router(invoices.router)
app.include_router(transmissions.router)
app.include_router(audit.router)


def custom_openapi() -> dict[str, Any]:
    """Déclare le Bearer JWT que le middleware exige déjà.

    Purement documentaire : l'authentification reste faite par
    TenantContextMiddleware, aucune dépendance FastAPI n'est ajoutée aux
    routes. Ce schéma fait seulement apparaître le bouton Authorize sur
    /docs et marque les routes protégées, en miroir de EXEMPT_PATHS —
    unique source de vérité de ce qui est ouvert.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("securitySchemes", {})[BEARER_SCHEME_NAME] = (
        BEARER_SCHEME
    )
    for path, operations in schema.get("paths", {}).items():
        if path in EXEMPT_PATHS:
            continue
        for method, operation in operations.items():
            if method.lower() in _OPERATION_KEYS:
                operation["security"] = [{BEARER_SCHEME_NAME: []}]

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.exception_handler(MissingTenantContextError)
async def missing_tenant_handler(request: Request, exc: MissingTenantContextError) -> JSONResponse:
    # Ne devrait jamais arriver : le middleware refuse déjà les requêtes sans
    # tenant. Ce handler est la ceinture applicative si une route contourne
    # le middleware par erreur.
    return JSONResponse({"detail": "Contexte tenant manquant."}, status_code=500)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
