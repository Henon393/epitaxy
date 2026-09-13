"""Familles de refresh tokens, révocables, dans Redis.

Chaque login ouvre une « famille » (une session) : la clé Redis mémorise le
seul jti de refresh actuellement valide pour cette famille. La rotation
remplace ce jti ; présenter un jti signé mais qui n'est plus le courant
signale un vol (réutilisation d'un token déjà consommé) et révoque la
famille entière, y compris le refresh le plus récent.
"""

from app.redis_client import get_redis

_PREFIX = "refresh_family:"


def register_family(family_id: str, jti: str, ttl_seconds: int) -> None:
    get_redis().set(_PREFIX + family_id, jti, ex=ttl_seconds)


def rotate_refresh(family_id: str, presented_jti: str, new_jti: str, ttl_seconds: int) -> bool:
    """Retourne False si la famille est morte ou si la réutilisation d'un
    ancien jti est détectée (la famille est alors révoquée sur-le-champ)."""
    redis_client = get_redis()
    key = _PREFIX + family_id
    current = redis_client.get(key)
    if current is None:
        # Famille expirée ou déjà révoquée.
        return False
    if current != presented_jti:
        # Réutilisation d'un refresh déjà consommé : session considérée
        # comme compromise, on tue toute la famille.
        redis_client.delete(key)
        return False
    redis_client.set(key, new_jti, ex=ttl_seconds)
    return True


def revoke_family(family_id: str) -> None:
    get_redis().delete(_PREFIX + family_id)
