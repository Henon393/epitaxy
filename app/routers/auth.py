"""Signup, login et refresh.

Ces routes s'exécutent avant tout contexte tenant de confiance : le contexte
RLS y vient de la revendication du client (motif « le contexte vient de la
revendication »). Il ne sert que de périmètre de recherche — jamais de
preuve : c'est argon2 qui authentifie.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.config import get_settings
from app.db import session_for_tenant
from app.models import Role, Tenant, User
from app.rate_limit import enforce_login_rate_limit
from app.schemas import LoginRequest, RefreshRequest, SignupRequest, SignupResponse, TokenPair
from app.security import (
    DUMMY_HASH,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.token_store import register_family, revoke_family, rotate_refresh

router = APIRouter(prefix="/auth", tags=["auth"])


def _invalid_credentials() -> HTTPException:
    # Message unique pour « utilisateur inexistant », « inactif » et
    # « mauvais mot de passe » : rien à énumérer.
    return HTTPException(status_code=401, detail="Identifiants invalides.")


def _open_session(user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> TokenPair:
    family_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    register_family(family_id, jti, get_settings().refresh_token_ttl_seconds)
    return TokenPair(
        access_token=create_access_token(user_id, tenant_id, role),
        refresh_token=create_refresh_token(user_id, tenant_id, family_id, jti),
    )


@router.post("/signup", response_model=SignupResponse, status_code=201)
def signup(payload: SignupRequest) -> SignupResponse:
    # L'UUID du tenant est généré ici puis posé en contexte AVANT les INSERT :
    # la policy RLS (id = current_setting) passe naturellement, sans aucun
    # contournement — la requête « devient » le tenant qu'elle crée.
    tenant_id = uuid.uuid4()
    with session_for_tenant(tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=payload.tenant_name))
        user = User(
            tenant_id=tenant_id,
            email=payload.email.strip().lower(),
            password_hash=hash_password(payload.password),
            role=Role.admin,
        )
        session.add(user)
        session.flush()
        user_id = user.id

    tokens = _open_session(user_id, tenant_id, Role.admin)
    return SignupResponse(tenant_id=tenant_id, user_id=user_id, **tokens.model_dump())


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, request: Request) -> TokenPair:
    client_ip = request.client.host if request.client else "inconnu"
    enforce_login_rate_limit(client_ip, payload.email)

    email = payload.email.strip().lower()
    with session_for_tenant(payload.tenant_id) as session:
        user = session.scalar(select(User).where(User.email == email))
        found = user is not None and user.is_active

        if not found:
            # Anti-énumération par timing : payer le coût argon2 même quand
            # l'utilisateur n'existe pas, pour un temps de réponse identique
            # au cas « mauvais mot de passe ».
            verify_password(DUMMY_HASH, payload.password)
            raise _invalid_credentials()
        if not verify_password(user.password_hash, payload.password):
            raise _invalid_credentials()

        return _open_session(user.id, user.tenant_id, user.role)


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="Refresh token invalide.") from exc

    new_jti = str(uuid.uuid4())
    settings = get_settings()
    if not rotate_refresh(
        claims["family_id"], claims["jti"], new_jti, settings.refresh_token_ttl_seconds
    ):
        # Famille expirée, révoquée, ou réutilisation détectée (et alors
        # toute la famille vient d'être tuée par rotate_refresh).
        raise HTTPException(status_code=401, detail="Session révoquée, reconnectez-vous.")

    tenant_id = uuid.UUID(claims["tenant_id"])
    user_id = uuid.UUID(claims["sub"])
    with session_for_tenant(tenant_id) as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            revoke_family(claims["family_id"])
            raise HTTPException(status_code=401, detail="Session révoquée, reconnectez-vous.")
        role = user.role

    return TokenPair(
        access_token=create_access_token(user_id, tenant_id, role),
        refresh_token=create_refresh_token(user_id, tenant_id, claims["family_id"], new_jti),
    )
