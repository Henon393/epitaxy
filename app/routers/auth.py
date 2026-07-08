"""Signup, login et refresh.

Ces routes s'exécutent avant tout contexte tenant de confiance : le contexte
RLS y vient de la revendication du client (motif « le contexte vient de la
revendication »). Il ne sert que de périmètre de recherche — jamais de
preuve : c'est argon2 qui authentifie.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.audit import log_unattributed, record
from app.config import get_settings
from app.db import session_for_tenant
from app.mfa import SecondFactorError, verify_second_factor
from app.models import AuditAction, Role, Tenant, User
from app.rate_limit import enforce_login_rate_limit, enforce_mfa_rate_limit
from app.redis_client import get_redis
from app.schemas import (
    LoginRequest,
    MfaChallengeOut,
    MfaVerifyIn,
    RefreshRequest,
    SignupRequest,
    SignupResponse,
    TokenPair,
)
from app.security import (
    DUMMY_HASH,
    TokenError,
    create_access_token,
    create_mfa_token,
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


def _open_session(
    user_id: uuid.UUID, tenant_id: uuid.UUID, role: str, session_version: int
) -> TokenPair:
    family_id = str(uuid.uuid4())
    jti = str(uuid.uuid4())
    register_family(family_id, jti, get_settings().refresh_token_ttl_seconds)
    return TokenPair(
        access_token=create_access_token(user_id, tenant_id, role, session_version),
        refresh_token=create_refresh_token(user_id, tenant_id, family_id, jti, session_version),
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

    tokens = _open_session(user_id, tenant_id, Role.admin, session_version=0)
    return SignupResponse(tenant_id=tenant_id, user_id=user_id, **tokens.model_dump())


@router.post("/login", response_model=TokenPair | MfaChallengeOut)
def login(payload: LoginRequest, request: Request) -> TokenPair | MfaChallengeOut:
    client_ip = request.client.host if request.client else "inconnu"
    email = payload.email.strip().lower()

    try:
        enforce_login_rate_limit(client_ip, payload.email)
    except HTTPException:
        # Le tenant revendiqué n'est ni vérifié ni digne de confiance à ce
        # stade : log applicatif, jamais audit_log.
        log_unattributed(
            AuditAction.login_rate_limited,
            ip=client_ip,
            email=email,
            tenant_revendique=payload.tenant_id,
        )
        raise

    # Les échecs sont audités DANS la session puis le 401 est levé APRÈS le
    # commit du bloc with : lever dans le bloc annulerait la ligne d'audit.
    succes = False
    tenant_inconnu = False
    mfa_pending = False
    with session_for_tenant(payload.tenant_id) as session:
        if session.get(Tenant, payload.tenant_id) is None:
            # Tenant inexistant : parité de timing quand même, et pas de
            # ligne d'audit (aucun tenant existant à qui la rattacher).
            verify_password(DUMMY_HASH, payload.password)
            tenant_inconnu = True
        else:
            user = session.scalar(select(User).where(User.email == email))
            if user is None or not user.is_active:
                # Anti-énumération par timing : payer le coût argon2 même
                # quand l'utilisateur n'existe pas, pour un temps de réponse
                # identique au cas « mauvais mot de passe ».
                verify_password(DUMMY_HASH, payload.password)
                record(
                    session,
                    AuditAction.login_failed,
                    actor_id=None,
                    tenant_id=payload.tenant_id,
                    metadata={"email": email, "ip": client_ip},
                )
            elif not verify_password(user.password_hash, payload.password):
                record(
                    session,
                    AuditAction.login_failed,
                    actor_id=user.id,
                    tenant_id=payload.tenant_id,
                    metadata={"email": email, "ip": client_ip},
                )
            elif user.mfa_enabled:
                # Authentification PARTIELLE : mot de passe validé, second
                # facteur exigé. Aucune session ouverte, pas de
                # login_succeeded — le jeton intermédiaire type="mfa"
                # n'ouvre rien (le middleware n'accepte que type="access").
                mfa_pending = True
                user_id, session_version = user.id, user.session_version
            else:
                record(
                    session,
                    AuditAction.login_succeeded,
                    actor_id=user.id,
                    tenant_id=payload.tenant_id,
                    metadata={"ip": client_ip},
                )
                user_id, role, session_version = user.id, user.role, user.session_version
                succes = True

    if tenant_inconnu:
        log_unattributed(
            AuditAction.login_failed,
            ip=client_ip,
            email=email,
            tenant_revendique=payload.tenant_id,
        )
    if mfa_pending:
        return MfaChallengeOut(
            mfa_token=create_mfa_token(user_id, payload.tenant_id, session_version)
        )
    if not succes:
        raise _invalid_credentials()
    return _open_session(user_id, payload.tenant_id, role, session_version)


@router.post("/mfa/verify", response_model=TokenPair)
def verify_mfa(payload: MfaVerifyIn, request: Request) -> TokenPair:
    """Échange du jeton intermédiaire + code (TOTP ou secours) contre la
    session complète. Jeton à usage unique, rate limité aux deux fenêtres."""
    client_ip = request.client.host if request.client else "inconnu"
    try:
        claims = decode_token(payload.mfa_token, expected_type="mfa")
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="Jeton MFA invalide ou expiré.") from exc

    user_id = uuid.UUID(claims["sub"])
    tenant_id = uuid.UUID(claims["tenant_id"])
    enforce_mfa_rate_limit(client_ip, str(user_id))

    succes = False
    method: str | None = None
    with session_for_tenant(tenant_id) as session:
        user = session.get(User, user_id)
        if (
            user is None
            or not user.is_active
            or not user.mfa_enabled
            or claims["sv"] != user.session_version
        ):
            # sv obsolète : une (dés)activation s'est intercalée depuis le
            # mot de passe — le challenge en cours meurt avec les sessions.
            raise HTTPException(status_code=401, detail="Second facteur invalide.")
        try:
            method = verify_second_factor(session, user, payload.code)
        except SecondFactorError:
            record(
                session,
                AuditAction.mfa_failed,
                actor_id=user.id,
                tenant_id=tenant_id,
                metadata={"ip": client_ip, "context": "login"},
            )
        else:
            record(
                session,
                AuditAction.login_succeeded,
                actor_id=user.id,
                tenant_id=tenant_id,
                metadata={"ip": client_ip, "mfa": "true", "method": method},
            )
            if method == "backup":
                # Événement dédié : un code de secours consommé signale un
                # second facteur perdu ou indisponible.
                record(
                    session,
                    AuditAction.mfa_backup_code_used,
                    actor_id=user.id,
                    tenant_id=tenant_id,
                    metadata={"ip": client_ip},
                )
            role, session_version = user.role, user.session_version
            succes = True

    if not succes:
        raise HTTPException(status_code=401, detail="Second facteur invalide.")

    # Usage unique du jeton intermédiaire, consommé au SUCCÈS seulement
    # (un code faux ne brûle pas le challenge ; le rate limit borne les
    # essais). SETNX : le premier échange gagne, tout rejeu → 401.
    consumed = get_redis().set(
        f"mfa_token_used:{claims['jti']}",
        "1",
        nx=True,
        ex=get_settings().mfa_token_ttl_seconds,
    )
    if not consumed:
        raise HTTPException(status_code=401, detail="Jeton MFA déjà utilisé.")
    return _open_session(user_id, tenant_id, role, session_version)


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
        if claims["sv"] != user.session_version:
            # Sessions invalidées depuis l'émission (activation ou
            # désactivation du MFA, plus tard changement de mot de passe) :
            # le refresh meurt immédiatement, la famille avec.
            revoke_family(claims["family_id"])
            raise HTTPException(status_code=401, detail="Session révoquée, reconnectez-vous.")
        role, session_version = user.role, user.session_version

    return TokenPair(
        access_token=create_access_token(user_id, tenant_id, role, session_version),
        refresh_token=create_refresh_token(
            user_id, tenant_id, claims["family_id"], new_jti, session_version
        ),
    )
