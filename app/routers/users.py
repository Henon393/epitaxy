from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_current_tenant, get_db
from app.identity import CurrentUser, get_current_user, require_role
from app.models import Role, User
from app.schemas import MeOut, UserCreate, UserOut
from app.security import hash_password

router = APIRouter(tags=["users"])


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.admin))],
)
def create_user(payload: UserCreate, db: Annotated[Session, Depends(get_db)]) -> User:
    user = User(
        tenant_id=get_current_tenant(),
        email=payload.email.strip().lower(),
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="Un utilisateur avec cet email existe déjà."
        ) from exc
    return user


@router.get("/me", response_model=MeOut)
def me() -> MeOut:
    user: CurrentUser | None = get_current_user()
    if user is None:
        # Atteignable uniquement via le repli X-Tenant-Id de dev, qui pose un
        # tenant sans identité.
        raise HTTPException(status_code=401, detail="Authentification requise.")
    return MeOut(user_id=user.id, tenant_id=user.tenant_id, role=Role(user.role))
