"""Ingestion des statuts rapportés par la PA, enregistreur, pas gardien.

Les statuts sont des faits rapportés par la PA, qui fait foi : une séquence
hors du graphe attendu est un bug de la PA ou une lacune de notre modèle,
dans les deux cas une information à consigner, jamais à rejeter. Le graphe
de 5a (validate_transition) n'est plus une porte mais un CLASSIFIEUR :
hors graphe => événement enregistré avec out_of_graph = true, audité en
anomalie et signalé au log sécurité.

Reste strictement validé, car c'est nous qui l'initions ou une exigence de
complétude légale : premier événement deposee (à la soumission), montant et
date obligatoires sur encaissee.

Idempotence : un événement PA (event_ref) ne s'enregistre qu'une fois,
vérification applicative puis index unique partiel en base sous savepoint,
ce qui règle aussi la course worker / refresh manuel.

Acteurs : source "manual" => acteur = utilisateur authentifié (ContextVar) ;
source "worker" => actor_id NULL passé EXPLICITEMENT, convention actée :
sur les actions transmission_*, NULL signifie acteur système (le refresh
manuel passe toujours par le middleware authentifié).
"""

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record
from app.models import AuditAction, PaStatusEvent, PaTransmission
from app.pa import InvalidTransitionError, PaStatus, StatusInfo, validate_transition

security_logger = logging.getLogger("app.security")

Source = Literal["manual", "worker"]


@dataclass(frozen=True)
class IngestResult:
    outcome: Literal["duplicate", "unchanged", "recorded", "anomaly_recorded"]
    position: int | None = None
    out_of_graph: bool = False


def ingest_status(
    session: Session,
    transmission: PaTransmission,
    info: StatusInfo,
    *,
    source: Source,
) -> IngestResult:
    last_event = transmission.events[-1]

    # Idempotence : événement PA déjà enregistré => aucune écriture.
    if info.event_ref is not None and any(
        event.pa_event_ref == info.event_ref for event in transmission.events
    ):
        return IngestResult(outcome="duplicate")
    # Filet legacy (connecteur sans event_ref) : même statut => rien à écrire.
    if info.event_ref is None and info.status == last_event.status:
        return IngestResult(outcome="unchanged")

    # Complétude légale, pas sémantique de séquence : reste bloquant.
    if info.status == PaStatus.encaissee and (info.paid_amount is None or info.paid_at is None):
        raise ValueError("Statut encaissée : montant et date de paiement obligatoires.")

    # Le graphe classifie, il ne rejette plus.
    out_of_graph = False
    try:
        validate_transition(PaStatus(last_event.status), info.status)
    except InvalidTransitionError:
        out_of_graph = True

    event = PaStatusEvent(
        tenant_id=transmission.tenant_id,
        transmission_id=transmission.id,
        position=last_event.position + 1,
        status=info.status,
        paid_amount=info.paid_amount,
        paid_at=info.paid_at,
        reason=info.reason,
        pa_event_ref=info.event_ref,
        out_of_graph=out_of_graph,
    )
    try:
        # Savepoint : la course worker / refresh manuel se résout sur les
        # contraintes d'unicité (event_ref, position), le perdant sort en
        # duplicate, zéro doublon, sans invalider la transaction porteuse.
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        return IngestResult(outcome="duplicate")

    # Audit en DERNIER : la tête de chaîne reste le dernier verrou pris,
    # ordre uniforme avec 3b et 4a, pas de deadlock.
    audit_actor: dict = {"actor_id": None} if source == "worker" else {}
    metadata = {
        "from": last_event.status,
        "to": str(info.status),
        "source": "pa_worker" if source == "worker" else "manual",
        **({"event_ref": info.event_ref} if info.event_ref else {}),
        **({"paid_amount": str(info.paid_amount)} if info.paid_amount is not None else {}),
        **({"paid_at": info.paid_at.isoformat()} if info.paid_at is not None else {}),
    }
    record(
        session,
        AuditAction.transmission_status_changed,
        target_type="pa_transmission",
        target_id=transmission.id,
        metadata=metadata,
        tenant_id=transmission.tenant_id,
        **audit_actor,
    )
    if out_of_graph:
        record(
            session,
            AuditAction.transmission_anomaly_detected,
            target_type="pa_transmission",
            target_id=transmission.id,
            metadata=metadata,
            tenant_id=transmission.tenant_id,
            **audit_actor,
        )
        security_logger.warning(
            "sequence de statuts hors graphe : transmission=%s %s -> %s (event_ref=%s)",
            transmission.id,
            last_event.status,
            info.status,
            info.event_ref,
        )
        return IngestResult(outcome="anomaly_recorded", position=event.position, out_of_graph=True)
    return IngestResult(outcome="recorded", position=event.position)
