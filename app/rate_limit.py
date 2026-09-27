"""Rate limiting du login : fenêtres fixes dans Redis.

Deux fenêtres cumulées sur la connexion : par couple (IP, email), et par
IP seule. La première protège un compte donné contre la force brute ; la
seconde s'oppose à la pulvérisation, où un mot de passe courant est
essayé sur de nombreux comptes depuis la même adresse, cas que la clé par
couple laisse entièrement passer.

Aucune fenêtre par email seul, volontairement : elle permettrait de
verrouiller le compte d'autrui en brûlant quelques essais, un déni de
service que le découpage actuel évite.
"""

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


def _cle_echecs_par_ip(client_ip: str) -> str:
    return f"rl:login:ip:{client_ip}"


def enforce_login_rate_limit(client_ip: str, email: str) -> None:
    settings = get_settings()
    _enforce_window(
        f"rl:login:{client_ip}:{email.strip().lower()}",
        settings.login_rate_limit_attempts,
        settings.login_rate_limit_window_seconds,
    )
    # Lecture passive, sans incrémenter : le quota par adresse ne compte
    # que les ÉCHECS, comptabilisés après coup par record_login_failure.
    # Compter toutes les tentatives pénaliserait un bureau entier derrière
    # une adresse partagée, dont les connexions réussissent.
    echecs = get_redis().get(_cle_echecs_par_ip(client_ip))
    if echecs is not None and int(echecs) >= settings.login_ip_rate_limit_attempts:
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives, réessayez plus tard.",
        )


def record_login_failure(client_ip: str) -> None:
    """Compte un échec de connexion pour l'adresse appelante.

    Appelée au seul point d'échec du login, donc jamais sur une connexion
    réussie ni sur un second facteur en attente, où le mot de passe était
    bon.
    """
    settings = get_settings()
    cle = _cle_echecs_par_ip(client_ip)
    redis_client = get_redis()
    echecs = redis_client.incr(cle)
    if echecs == 1:
        redis_client.expire(cle, settings.login_ip_rate_limit_window_seconds)


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
