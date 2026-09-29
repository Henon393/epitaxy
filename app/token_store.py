"""Familles de refresh tokens, révocables, dans Redis.

Chaque login ouvre une « famille » (une session) : la clé Redis mémorise le
seul jti de refresh actuellement valide pour cette famille. La rotation
remplace ce jti ; présenter un jti signé mais qui n'est plus le courant
signale un vol (réutilisation d'un token déjà consommé) et révoque la
famille entière, y compris le refresh le plus récent.
"""

from functools import partial

import anyio.to_thread

from app.redis_client import get_redis

_PREFIX = "refresh_family:"

# Révocation immédiate des jetons d'accès déjà émis.
#
# Le middleware ne consulte pas la base : il fait confiance aux claims du
# jeton, rôle compris. Incrémenter session_version ne suffit donc pas, ce
# champ n'est vérifié qu'au rafraîchissement. Sans le présent mécanisme, un
# compte désactivé garderait tous ses droits jusqu'à l'expiration de son
# jeton d'accès, et un admin rétrogradé resterait admin d'autant.
#
# On mémorise ici la version de session minimale acceptée pour un compte.
# Le TTL vaut la durée de vie d'un jeton d'accès : au-delà, tout jeton
# portant une version antérieure est de toute façon expiré, la clé peut
# donc disparaître d'elle-même.
_PREFIX_REVOCATION = "user_revoked:"


def revoke_sessions_before(user_id: str, session_version: int, ttl_seconds: int) -> None:
    """Refuse désormais tout jeton d'accès antérieur à ``session_version``."""
    get_redis().set(_PREFIX_REVOCATION + str(user_id), session_version, ex=ttl_seconds)


async def minimum_session_version(user_id: str) -> int | None:
    """Version de session minimale acceptée, ``None`` si rien n'est révoqué.

    Appelée depuis le middleware, qui est une coroutine : la lecture part
    dans un thread pour ne pas figer la boucle d'événements. On réutilise le
    client synchrone plutôt qu'un client asyncio, dont le pool se lie à la
    boucle qui l'a créé et casse dès que celle-ci est remplacée.

    Lève l'erreur Redis telle quelle : l'appelant doit refuser la requête
    plutôt que d'accorder un accès peut-être coupé (fail-closed).
    """
    valeur = await anyio.to_thread.run_sync(
        partial(get_redis().get, _PREFIX_REVOCATION + str(user_id))
    )
    return int(valeur) if valeur is not None else None


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
