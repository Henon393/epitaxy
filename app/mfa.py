"""Second facteur TOTP : chiffrement du secret au repos, vérification.

Chiffrement : Fernet (AES-128-CBC + HMAC-SHA256, encrypt-then-MAC, IV
aléatoire, format versionné) en MultiFernet, la première clé de
APP_MFA_ENCRYPTION_KEYS chiffre, toutes déchiffrent. Rotation : nouvelle
clé en tête, re-chiffrement hors ligne par migrator (MultiFernet.rotate),
retrait de l'ancienne. Le TODO(2b) posé sur mfa_secret à l'étape 2 est
fermé : le secret n'existe en clair que dans l'URI d'enrôlement, retournée
une fois.

Anti-rejeu : matching_timestep retourne le PAS DE TEMPS apparié (comparaison
à temps constant), mémorisé sur l'utilisateur par UPDATE conditionnel, un
code accepté n'est pas rejouable dans sa fenêtre, ni aucun code plus ancien.

Limite documentée : la perte du second facteur sans code de secours n'a pas
de procédure de récupération dans ce périmètre.
"""

import hmac
import io
import secrets
import uuid
from datetime import UTC, datetime
from functools import lru_cache

import pyotp
import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MfaBackupCode, User
from app.security import hash_password, verify_password

TOTP_STEP_SECONDS = 30
# Tolérance de dérive d'horloge : pas courant, précédent, suivant.
TOTP_WINDOW_OFFSETS = (0, -1, 1)
BACKUP_CODE_COUNT = 10

ISSUER = "epitaxy"


class SecondFactorError(Exception):
    """Code TOTP ou code de secours invalide, rejoué ou consommé."""


@lru_cache
def _fernet() -> MultiFernet:
    keys = [key.strip() for key in get_settings().mfa_encryption_keys.split(",") if key.strip()]
    return MultiFernet([Fernet(key.encode()) for key in keys])


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(token: str) -> str:
    # PAS de ttl ici : l'horodatage embarqué dans le jeton Fernet ne doit
    # jamais faire expirer un secret au repos. Ne pas en ajouter un
    # « par sécurité ».
    return _fernet().decrypt(token.encode()).decode()


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


def matching_timestep(secret: str, code: str, at: datetime | None = None) -> int | None:
    """Pas de temps de la fenêtre dont le code correspond, ou None.

    pyotp.verify ne rend qu'un booléen ; l'anti-rejeu exige le pas de temps
    apparié. Comparaison à temps constant sur chaque candidat.
    """
    totp = pyotp.TOTP(secret)
    current_timestep = int((at or datetime.now(UTC)).timestamp()) // TOTP_STEP_SECONDS
    for offset in TOTP_WINDOW_OFFSETS:
        candidate = current_timestep + offset
        expected = totp.at(candidate * TOTP_STEP_SECONDS)
        if hmac.compare_digest(expected, code):
            return candidate
    return None


def generate_backup_codes() -> list[str]:
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(BACKUP_CODE_COUNT)]


def verify_second_factor(
    session: Session, user: User, code: str, *, allow_backup: bool = True
) -> str:
    """Vérifie un code TOTP (anti-rejeu compris) ou un code de secours.

    Retourne "totp" ou "backup" ; lève SecondFactorError sinon.
    """
    secret = decrypt_totp_secret(user.mfa_secret)
    timestep = matching_timestep(secret, code)
    if timestep is not None:
        # Anti-rejeu, garde concurrente : seul le premier UPDATE gagne, un
        # code déjà consommé (timestep <= mémorisé) ne repasse pas.
        updated = session.execute(
            update(User)
            .where(
                User.id == user.id,
                or_(User.mfa_last_timestep.is_(None), User.mfa_last_timestep < timestep),
            )
            .values(mfa_last_timestep=timestep)
        ).rowcount
        if updated == 1:
            return "totp"
        raise SecondFactorError("Code TOTP déjà utilisé dans sa fenêtre.")

    if allow_backup:
        backup_codes = session.scalars(
            select(MfaBackupCode).where(
                MfaBackupCode.user_id == user.id, MfaBackupCode.used_at.is_(None)
            )
        )
        for backup in backup_codes:
            if verify_password(backup.code_hash, code):
                # Usage unique, garde concurrente identique.
                consumed = session.execute(
                    update(MfaBackupCode)
                    .where(MfaBackupCode.id == backup.id, MfaBackupCode.used_at.is_(None))
                    .values(used_at=datetime.now(UTC))
                ).rowcount
                if consumed == 1:
                    return "backup"
    raise SecondFactorError("Second facteur invalide.")


def store_backup_codes(session: Session, user: User) -> list[str]:
    codes = generate_backup_codes()
    for code in codes:
        session.add(
            MfaBackupCode(
                id=uuid.uuid4(),
                tenant_id=user.tenant_id,
                user_id=user.id,
                code_hash=hash_password(code),
            )
        )
    return codes
