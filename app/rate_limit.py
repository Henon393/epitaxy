"""Rate limiting du login : fenêtre fixe dans Redis, clé (IP, email)."""

from fastapi import HTTPException

from app.config import get_settings
from app.redis_client import get_redis


def enforce_login_rate_limit(client_ip: str, email: str) -> None:
    settings = get_settings()
    key = f"rl:login:{client_ip}:{email.strip().lower()}"
    redis_client = get_redis()
    attempts = redis_client.incr(key)
    if attempts == 1:
        redis_client.expire(key, settings.login_rate_limit_window_seconds)
    if attempts > settings.login_rate_limit_attempts:
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives de connexion, réessayez plus tard.",
        )
