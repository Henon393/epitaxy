import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.db import get_current_tenant, get_db
from app.identity import require_role
from app.models import AuditAction, Customer, Role
from app.schemas import CustomerCreate, CustomerOut

router = APIRouter(tags=["customers"])


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Annotated[Session, Depends(get_db)]) -> list[Customer]:
    # Pas de filtre tenant_id ici : c'est la RLS qui filtre. Le test
    # d'isolation le prouve, une fuite serait un bug de la base, pas
    # un oubli de WHERE. Lecture ouverte à tout tenant authentifié.
    return list(db.scalars(select(Customer).order_by(Customer.created_at)))


@router.post(
    "/customers",
    response_model=CustomerOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def create_customer(payload: CustomerCreate, db: Annotated[Session, Depends(get_db)]) -> Customer:
    customer = Customer(tenant_id=get_current_tenant(), **payload.model_dump())
    db.add(customer)
    db.flush()
    # Même transaction que la mutation : pas de customer créé sans sa ligne
    # d'audit, ni l'inverse (fail-closed).
    record(db, AuditAction.customer_created, target_type="customer", target_id=customer.id)
    return customer


@router.put(
    "/customers/{customer_id}",
    response_model=CustomerOut,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def update_customer(
    customer_id: uuid.UUID, payload: CustomerCreate, db: Annotated[Session, Depends(get_db)]
) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")
    for field, value in payload.model_dump().items():
        setattr(customer, field, value)
    db.flush()
    return customer


@router.delete(
    "/customers/{customer_id}",
    status_code=204,
    dependencies=[Depends(require_role(Role.admin))],
)
def delete_customer(customer_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> None:
    customer = db.get(Customer, customer_id)
    if customer is None:
        # La RLS rend les clients des autres tenants invisibles : le 404
        # vaut aussi bien pour « inexistant » que pour « pas à vous ».
        raise HTTPException(status_code=404, detail="Client introuvable.")
    db.delete(customer)
    db.flush()
    record(db, AuditAction.customer_deleted, target_type="customer", target_id=customer_id)
