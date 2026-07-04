from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_current_tenant, get_db
from app.identity import require_role
from app.models import Customer, Role
from app.schemas import CustomerCreate, CustomerOut

router = APIRouter(tags=["customers"])


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Annotated[Session, Depends(get_db)]) -> list[Customer]:
    # Pas de filtre tenant_id ici : c'est la RLS qui filtre. Le test
    # d'isolation le prouve — une fuite serait un bug de la base, pas
    # un oubli de WHERE. Lecture ouverte à tout tenant authentifié.
    return list(db.scalars(select(Customer).order_by(Customer.created_at)))


@router.post(
    "/customers",
    response_model=CustomerOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def create_customer(payload: CustomerCreate, db: Annotated[Session, Depends(get_db)]) -> Customer:
    customer = Customer(
        tenant_id=get_current_tenant(),
        name=payload.name,
        email=payload.email,
    )
    db.add(customer)
    db.flush()
    return customer
