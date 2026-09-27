"""Worker de polling des statuts PA (RQ sur Redis).

Découverte BASE-FIRST : la base est la source de vérité. Le cycle appelle
pa_active_transmissions(), fonction SECURITY DEFINER étroite (propriété du
rôle NOLOGIN pa_scanner, policies ciblées en lecture seule) qui ne retourne
que des paires (tenant_id, transmission_id) actives, dernier statut non
terminal et encaissements non soldés. Redis ne sert qu'à RQ (queue,
retries) : le vider ne fait perdre aucune transmission.

Chaque job ouvre une session scopée tenant (SET LOCAL app.tenant_id) qui
couvre la RLS de toutes les écritures et le verrou de tête du chaînage
d'audit. Les événements ingérés ici portent l'acteur système (actor_id
NULL, source pa_worker), cf. app/pa_ingest.py.

Reprise sur échec : Retry RQ avec backoff ; la reprise ne crée aucun
doublon, l'idempotence par event_ref l'absorbe. L'API n'attend jamais le
worker (la soumission reste synchrone, le refresh manuel reste disponible).

Limite documentée : une transmission au paiement partiel jamais soldé reste
active indéfiniment et sera pollée à chaque cycle, l'arrêt de suivi par
expiration ou décision est hors périmètre.

Lancement (le worker RQ exige un environnement POSIX, conteneur ou WSL sur
ce poste) : `rq worker pa_polling --with-scheduler`, puis amorcer le cycle
avec `python -c "from app.worker import poll_cycle; poll_cycle()"`.
"""

import logging
import uuid
from datetime import timedelta
from functools import lru_cache

from redis import Redis
from rq import Queue, Retry
from sqlalchemy import text

from app.config import get_settings
from app.db import engine, session_for_tenant
from app.models import PaTransmission
from app.pa import get_pa_connector
from app.pa_ingest import ingest_status

security_logger = logging.getLogger("app.security")

QUEUE_NAME = "pa_polling"


@lru_cache
def _rq_connection() -> Redis:
    # Connexion dédiée à RQ : pas de decode_responses (RQ manipule des bytes).
    return Redis.from_url(get_settings().redis_url)


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=_rq_connection())


def list_active_transmissions() -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Paires (tenant_id, transmission_id) actives, depuis la base seule."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tenant_id, transmission_id FROM pa_active_transmissions()")
        ).fetchall()
    return [(row.tenant_id, row.transmission_id) for row in rows]


def poll_transmission(tenant_id: str, transmission_id: str) -> str:
    """Job unitaire : interroge la PA et ingère, sous contexte tenant."""
    with session_for_tenant(uuid.UUID(tenant_id)) as session:
        transmission = session.get(PaTransmission, uuid.UUID(transmission_id))
        if transmission is None:
            return "introuvable"
        info = get_pa_connector().get_status(transmission.pa_transmission_ref)
        try:
            result = ingest_status(session, transmission, info, source="worker")
        except ValueError as exc:
            # Payload invalide (encaissée sans montant/date) : anomalie de
            # données, un retry n'y changerait rien, signalé, pas rejoué.
            security_logger.warning(
                "poll: payload invalide transmission=%s : %s", transmission_id, exc
            )
            return "payload_invalide"
    return result.outcome


def enqueue_polls() -> int:
    """Enfile un job par transmission active découverte en base."""
    settings = get_settings()
    queue = get_queue()
    pairs = list_active_transmissions()
    for tenant_id, transmission_id in pairs:
        queue.enqueue(
            poll_transmission,
            str(tenant_id),
            str(transmission_id),
            retry=Retry(max=settings.pa_poll_retry_max, interval=settings.pa_poll_retry_intervals),
        )
    return len(pairs)


def poll_cycle() -> int:
    """Cycle périodique auto-ré-enfilé (exécuté par un worker --with-scheduler)."""
    count = enqueue_polls()
    get_queue().enqueue_in(timedelta(seconds=get_settings().pa_poll_interval_seconds), poll_cycle)
    return count
