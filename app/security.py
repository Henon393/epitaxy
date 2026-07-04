"""Hachage argon2 et émission/vérification des JWT."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()

# Hash factice constant : le chemin « utilisateur inexistant » du login paie
# le même coût argon2 que le chemin « mauvais mot de passe », pour un temps de
# réponse indiscernable (anti-énumération par timing).
DUMMY_HASH = _hasher.hash("valeur-factice-anti-enumeration")

ALGORITHM = "HS256"


class TokenError(Exception):
    """Token invalide, expiré, mal typé ou mal signé."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _encode(claims: dict[str, Any], ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    claims = {**claims, "iat": now, "exp": now + timedelta(seconds=ttl_seconds)}
    return jwt.encode(claims, get_settings().jwt_secret, algorithm=ALGORITHM)


def create_access_token(user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> str:
    return _encode(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role,
            "type": "access",
            "jti": str(uuid.uuid4()),
        },
        get_settings().access_token_ttl_seconds,
    )


def create_refresh_token(user_id: uuid.UUID, tenant_id: uuid.UUID, family_id: str, jti: str) -> str:
    return _encode(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "type": "refresh",
            "family_id": family_id,
            "jti": jti,
        },
        get_settings().refresh_token_ttl_seconds,
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """Vérifie signature, expiration et type.

    ``algorithms=[ALGORITHM]`` est imposé côté serveur : l'``alg`` annoncé
    dans le header du token n'est jamais utilisé pour choisir l'algorithme
    (rejette « none », HS512, ou toute confusion d'algorithme).
    """
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub", "tenant_id", "type", "jti"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError(str(exc)) from exc
    if claims["type"] != expected_type:
        raise TokenError(f"type de token inattendu : {claims['type']!r}")
    return claims
