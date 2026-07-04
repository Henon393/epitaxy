from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.db import MissingTenantContextError
from app.middleware import TenantContextMiddleware
from app.routers import auth, company_profile, customers, invoices, users

app = FastAPI(title="Facturation — Solution Compatible (apprentissage)")
app.add_middleware(TenantContextMiddleware)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(customers.router)
app.include_router(company_profile.router)
app.include_router(invoices.router)


@app.exception_handler(MissingTenantContextError)
async def missing_tenant_handler(request: Request, exc: MissingTenantContextError) -> JSONResponse:
    # Ne devrait jamais arriver : le middleware refuse déjà les requêtes sans
    # tenant. Ce handler est la ceinture applicative si une route contourne
    # le middleware par erreur.
    return JSONResponse({"detail": "Contexte tenant manquant."}, status_code=500)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
