from typing import Annotated

from fastapi import Depends, FastAPI, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.db import MissingTenantContextError, get_current_tenant, get_db
from app.middleware import TenantContextMiddleware
from app.models import Customer
from app.schemas import CustomerCreate, CustomerOut

app = FastAPI(title="Facturation — Solution Compatible (apprentissage)")
app.add_middleware(TenantContextMiddleware)


@app.exception_handler(MissingTenantContextError)
async def missing_tenant_handler(request: Request, exc: MissingTenantContextError) -> JSONResponse:
    # Ne devrait jamais arriver : le middleware refuse déjà les requêtes sans
    # tenant. Ce handler est la ceinture applicative si une route contourne
    # le middleware par erreur.
    return JSONResponse({"detail": "Contexte tenant manquant."}, status_code=500)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Annotated[Session, Depends(get_db)]) -> list[Customer]:
    # Pas de filtre tenant_id ici : c'est la RLS qui filtre. Le test
    # d'isolation le prouve — une fuite serait un bug de la base, pas
    # un oubli de WHERE.
    return list(db.scalars(select(Customer).order_by(Customer.created_at)))


@app.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(payload: CustomerCreate, db: Annotated[Session, Depends(get_db)]) -> Customer:
    customer = Customer(
        tenant_id=get_current_tenant(),
        name=payload.name,
        email=payload.email,
    )
    db.add(customer)
    db.flush()
    return customer
