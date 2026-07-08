"""Enrôlement et gestion du second facteur TOTP (compte propre).

Activation prouvée : le MFA ne s'active qu'après vérification d'un premier
code valide. À l'activation ET à la désactivation, session_version est
incrémenté : les sessions ouvertes avant le changement meurent au prochain
rafraîchissement (les access tokens déjà émis restent valides jusqu'à leur
expiration courte, limite assumée : le middleware est sans accès base).

Limite documentée : la perte du second facteur sans code de secours n'a pas
de procédure de récupération dans ce périmètre.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.audit import record
from app.db import get_db, session_for_tenant
from app.identity import get_current_user
from app.mfa import (
    SecondFactorError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_totp_secret,
    matching_timestep,
    provisioning_uri,
    qr_svg,
    store_backup_codes,
    verify_second_factor,
)
from app.models import AuditAction, MfaBackupCode, User
from app.schemas import MfaActivateOut, MfaCodeIn, MfaEnrollOut

router = APIRouter(prefix="/mfa", tags=["mfa"])


def _own_user(db: Session) -> User:
    identity = get_current_user()
    if identity is None:
        raise HTTPException(status_code=401, detail="Authentification requise.")
    return db.get(User, identity.id)


def _audit_failure(user: User, context: str) -> None:
    """Audit d'échec dans une session DÉDIÉE, committée avant le 4xx : dans
    la session de la requête, le rollback déclenché par l'exception
    emporterait la ligne d'audit (leçon du login, étape 3)."""
    with session_for_tenant(user.tenant_id) as audit_session:
        record(
            audit_session,
            AuditAction.mfa_failed,
            target_type="user",
            target_id=user.id,
            metadata={"context": context},
            tenant_id=user.tenant_id,
        )


@router.post("/enroll", response_model=MfaEnrollOut)
def enroll(db: Annotated[Session, Depends(get_db)]) -> MfaEnrollOut:
    user = _own_user(db)
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA déjà activé.")

    # Ré-enrôlable tant que non activé. Le secret ne circule qu'ici, dans
    # l'URI retournée une fois ; en base, uniquement le jeton Fernet.
    secret = generate_totp_secret()
    user.mfa_secret = encrypt_totp_secret(secret)
    user.mfa_last_timestep = None
    db.flush()
    record(db, AuditAction.mfa_enrolled, target_type="user", target_id=user.id)
    uri = provisioning_uri(secret, user.email)
    return MfaEnrollOut(otpauth_uri=uri, qr_svg=qr_svg(uri))


@router.post("/activate", response_model=MfaActivateOut)
def activate(payload: MfaCodeIn, db: Annotated[Session, Depends(get_db)]) -> MfaActivateOut:
    user = _own_user(db)
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA déjà activé.")
    if user.mfa_secret is None:
        raise HTTPException(status_code=409, detail="Aucun enrôlement en cours.")

    # On n'active pas un second facteur qu'on n'a pas prouvé fonctionnel.
    secret = decrypt_totp_secret(user.mfa_secret)
    timestep = matching_timestep(secret, payload.code)
    if timestep is None:
        _audit_failure(user, "activation")
        raise HTTPException(status_code=422, detail="Code TOTP invalide : activation refusée.")

    user.mfa_enabled = True
    user.mfa_last_timestep = timestep  # le code d'activation est consommé
    # Invalidation des sessions ouvertes AVANT l'activation : un refresh
    # antérieur permettrait de continuer sans jamais passer le MFA.
    user.session_version += 1
    codes = store_backup_codes(db, user)
    db.flush()
    record(db, AuditAction.mfa_activated, target_type="user", target_id=user.id)
    # Affichés une seule fois, jamais restitués ensuite.
    return MfaActivateOut(backup_codes=codes)


@router.post("/disable", status_code=204)
def disable(payload: MfaCodeIn, db: Annotated[Session, Depends(get_db)]) -> None:
    user = _own_user(db)
    if not user.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA non activé.")
    try:
        verify_second_factor(db, user, payload.code)
    except SecondFactorError as exc:
        _audit_failure(user, "desactivation")
        raise HTTPException(status_code=401, detail="Second facteur invalide.") from exc

    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_last_timestep = None
    # Symétrique de l'activation : les sessions en cours meurent.
    user.session_version += 1
    db.execute(delete(MfaBackupCode).where(MfaBackupCode.user_id == user.id))
    db.flush()
    record(db, AuditAction.mfa_disabled, target_type="user", target_id=user.id)
