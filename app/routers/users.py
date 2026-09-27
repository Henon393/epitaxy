import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record
from app.config import get_settings
from app.db import get_current_tenant, get_db
from app.identity import CurrentUser, get_current_user, require_role
from app.models import AuditAction, Role, User
from app.routing import RouteValideeAvantReponse
from app.schemas import MeOut, UserCreate, UserOut, UserPatch
from app.security import hash_password
from app.token_store import revoke_sessions_before

router = APIRouter(tags=["users"], route_class=RouteValideeAvantReponse)


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
        # Aucun utilisateur n'a été créé : rien à tracer.
        raise HTTPException(
            status_code=409, detail="Un utilisateur avec cet email existe déjà."
        ) from exc
    # Même transaction que la création : pas d'utilisateur créé sans sa ligne
    # d'audit, ni l'inverse (fail-closed). L'acteur est l'administrateur qui
    # crée, lu du contexte, jamais le compte créé.
    record(
        db,
        AuditAction.user_created,
        target_type="user",
        target_id=user.id,
        metadata={"role": str(user.role)},
    )
    return user


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    dependencies=[Depends(require_role(Role.admin))],
)
def update_user(
    user_id: uuid.UUID, payload: UserPatch, db: Annotated[Session, Depends(get_db)]
) -> User:
    """Change le rôle d'un compte ou le désactive.

    Les sessions en cours du compte visé sont coupées immédiatement, pas au
    prochain rafraîchissement : sans cela, un compte compromis garderait ses
    droits jusqu'à l'expiration de son jeton d'accès.
    """
    user = db.get(User, user_id)
    if user is None:
        # La RLS rend les comptes des autres tenants invisibles : le 404 vaut
        # pour « inexistant » comme pour « pas à vous ».
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")

    role_avant = str(user.role)
    actif_avant = user.is_active
    role_apres = str(payload.role) if payload.role is not None else role_avant
    actif_apres = payload.is_active if payload.is_active is not None else actif_avant

    change_role = role_apres != role_avant
    change_activation = actif_apres != actif_avant
    if not change_role and not change_activation:
        # Rien à changer : ni mutation, ni trace, ni révocation.
        return user

    _garde_dernier_admin(db, user, role_apres=role_apres, actif_apres=actif_apres)

    user.role = role_apres
    user.is_active = actif_apres
    # Le refresh compare cette version à celle du jeton : la famille de
    # session meurt à la première tentative. La révocation Redis ci-dessous
    # ferme en plus la fenêtre du jeton d'accès déjà émis.
    user.session_version += 1
    db.flush()

    # Un fait, une entrée : un PATCH qui change les deux en écrit deux.
    if change_role:
        record(
            db,
            AuditAction.user_role_changed,
            target_type="user",
            target_id=user.id,
            metadata={"from": role_avant, "to": role_apres},
        )
    if change_activation:
        record(
            db,
            AuditAction.user_deactivated if not actif_apres else AuditAction.user_reactivated,
            target_type="user",
            target_id=user.id,
            metadata={"role": role_apres},
        )

    # Écrit AVANT le commit : si la transaction échoue ensuite, on aura
    # révoqué des sessions pour un changement annulé, ce qui oblige à se
    # reconnecter sans rien accorder. L'ordre inverse laisserait la fenêtre
    # ouverte en cas d'incident, ce qui serait un défaut de sécurité.
    try:
        revoke_sessions_before(
            str(user.id), user.session_version, get_settings().access_token_ttl_seconds
        )
    except RedisError as exc:
        # Sans révocation effective, la promesse de coupure immédiate n'est
        # pas tenue : on annule plutôt que de la laisser croire.
        raise HTTPException(
            status_code=503, detail="Révocation des sessions indisponible : changement annulé."
        ) from exc

    return user


def _garde_dernier_admin(db: Session, user: User, *, role_apres: str, actif_apres: bool) -> None:
    """Un tenant doit toujours conserver au moins un administrateur actif.

    Couvre d'un seul tenant les trois cas : l'admin qui se désactive, celui
    qui se rétrograde, et celui qui prive le tenant de son dernier autre
    administrateur.
    """
    restait_admin_actif = user.role == Role.admin and user.is_active
    reste_admin_actif = role_apres == Role.admin and actif_apres
    if not restait_admin_actif or reste_admin_actif:
        return

    autres = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.id != user.id, User.role == Role.admin, User.is_active.is_(True))
    )
    if not autres:
        raise HTTPException(
            status_code=409,
            detail="Dernier administrateur actif du tenant : changement refusé.",
        )


@router.get("/me", response_model=MeOut)
def me() -> MeOut:
    user: CurrentUser | None = get_current_user()
    if user is None:
        # Inatteignable en pratique : le middleware pose toujours l'identité
        # avec le contexte tenant. Ceinture applicative, pour qu'une route
        # exemptée par erreur renvoie 401 plutôt que 500.
        raise HTTPException(status_code=401, detail="Authentification requise.")
    return MeOut(user_id=user.id, tenant_id=user.tenant_id, role=Role(user.role))
