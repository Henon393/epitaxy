"""Rate limiting du login : fenêtre fixe dans Redis, clé (IP, email)."""

from fastapi import HTTPException

from app.config import get_settings
from app.redis_client import get_redis


def _enforce_window(key: str, attempts_allowed: int, window_seconds: int) -> None:
    redis_client = get_redis()
    attempts = redis_client.incr(key)
    if attempts == 1:
        redis_client.expire(key, window_seconds)
    if attempts > attempts_allowed:
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives, réessayez plus tard.",
        )


def enforce_login_rate_limit(client_ip: str, email: str) -> None:
    settings = get_settings()
    _enforce_window(
        f"rl:login:{client_ip}:{email.strip().lower()}",
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )


def enforce_mfa_rate_limit(client_ip: str, user_id: str) -> None:
    """Deux fenêtres cumulées sur la vérification du second facteur :
    par (IP, utilisateur), et par utilisateur SEUL, un brute-force
    distribué sur plusieurs IP ne contourne pas la seconde. Temporaires et
    auto-réinitialisées (TTL) : pas de verrouillage exploitable en déni de
    service contre la victime."""
    settings = get_settings()
    _enforce_window(
        f"rl:mfa:{client_ip}:{user_id}",
        settings.mfa_rate_limit_attempts,
        settings.mfa_rate_limit_window_seconds,
    )
    _enforce_window(
        f"rl:mfa:user:{user_id}",
        settings.mfa_user_rate_limit_attempts,
        settings.mfa_user_rate_limit_window_seconds,
    )
