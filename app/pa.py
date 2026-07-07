"""Connecteur Plateforme Agréée : port abstrait, mock, machine à états.

Cycle de vie (codes de la nomenclature DGFiP, spécifications externes 3.1).
Le graphe encode deux invariants :
- progression avant seulement, les statuts recommandés (recue, approuvee)
  étant facultatifs — une PA minimale saute directement de deposee à
  encaissee ou refusee ;
- la distinction technique/commercial est portée par le statut lui-même
  (rejetee 213 = rejet technique par la PA, refusee 210 = refus commercial
  par l'acheteur), pas par la position dans le graphe. Seule contrainte de
  position : rejetee n'existe qu'au stade du dépôt (sortant de deposee).

rejetee et refusee sont des puits terminaux : aucune transition sortante.
Le trigger de la migration 0006 encode le même graphe côté base.
"""

import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache

from app.config import get_settings


class PaStatus(enum.StrEnum):
    deposee = "deposee"
    recue = "recue"
    approuvee = "approuvee"
    refusee = "refusee"
    encaissee = "encaissee"
    rejetee = "rejetee"


# Codes de la nomenclature DGFiP (vérifiés contre les spécifications
# externes v3.1) : 4 obligatoires (200, 210, 212, 213), 2 recommandés.
PA_STATUS_CODES = {
    PaStatus.deposee: "200",
    PaStatus.recue: "202",
    PaStatus.approuvee: "205",
    PaStatus.refusee: "210",
    PaStatus.encaissee: "212",
    PaStatus.rejetee: "213",
}

TRANSITIONS: dict[PaStatus, frozenset[PaStatus]] = {
    PaStatus.deposee: frozenset(
        {PaStatus.recue, PaStatus.approuvee, PaStatus.encaissee, PaStatus.refusee, PaStatus.rejetee}
    ),
    PaStatus.recue: frozenset({PaStatus.approuvee, PaStatus.encaissee, PaStatus.refusee}),
    # Un refus qui contredirait une approbation explicite n'est pas modélisé
    # (litige/suspendue hors périmètre).
    PaStatus.approuvee: frozenset({PaStatus.encaissee}),
    # Paiements partiels successifs : encaissee est répétable.
    PaStatus.encaissee: frozenset({PaStatus.encaissee}),
    # Puits terminaux.
    PaStatus.refusee: frozenset(),
    PaStatus.rejetee: frozenset(),
}


class InvalidTransitionError(Exception):
    pass


def validate_transition(current: PaStatus, new: PaStatus) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"Transition de statut invalide : {current} -> {new}."
            + (" L'état est terminal." if not TRANSITIONS[current] else "")
        )


@dataclass(frozen=True)
class StatusInfo:
    status: PaStatus
    paid_amount: Decimal | None = None
    paid_at: date | None = None
    reason: str | None = None
    # Identifiant d'événement fourni par la PA : clé d'idempotence du poll
    # (5b). Deux polls du même événement portent le même event_ref ; deux
    # paiements distincts, deux event_ref.
    event_ref: str | None = None


@dataclass(frozen=True)
class SubmissionResult:
    transmission_ref: str
    initial: StatusInfo


class PaConnector(ABC):
    """Port vers la Plateforme Agréée. Le mock est l'implémentation par
    défaut ; la vraie PA se branchera par une nouvelle implémentation et
    une valeur de config, sans réécriture (règle CLAUDE.md : aucune
    transmission réelle tant que ce branchement n'est pas volontaire)."""

    @abstractmethod
    def submit(self, facturx_pdf: bytes, routing_identifier: str) -> SubmissionResult: ...

    @abstractmethod
    def get_status(self, transmission_ref: str) -> StatusInfo: ...


class MockPaConnector(PaConnector):
    """PA simulée en mémoire. Les tests pilotent les statuts via
    ``program_status`` pour exercer la machine à états."""

    def __init__(self) -> None:
        self._statuses: dict[str, StatusInfo] = {}

    def submit(self, facturx_pdf: bytes, routing_identifier: str) -> SubmissionResult:
        transmission_ref = uuid.uuid4().hex
        initial = StatusInfo(status=PaStatus.deposee, event_ref=uuid.uuid4().hex)
        self._statuses[transmission_ref] = initial
        return SubmissionResult(transmission_ref=transmission_ref, initial=initial)

    def get_status(self, transmission_ref: str) -> StatusInfo:
        return self._statuses[transmission_ref]

    def program_status(
        self,
        transmission_ref: str,
        status: PaStatus,
        paid_amount: Decimal | None = None,
        paid_at: date | None = None,
        reason: str | None = None,
    ) -> None:
        # event_ref généré AU MOMENT de la programmation, puis stable d'un
        # poll à l'autre : re-poller le même statut redonne le même
        # identifiant (idempotence), reprogrammer en crée un nouveau.
        self._statuses[transmission_ref] = StatusInfo(
            status=status,
            paid_amount=paid_amount,
            paid_at=paid_at,
            reason=reason,
            event_ref=uuid.uuid4().hex,
        )


@lru_cache
def get_pa_connector() -> PaConnector:
    connector = get_settings().pa_connector
    if connector == "mock":
        return MockPaConnector()
    raise ValueError(f"Connecteur PA inconnu : {connector!r}")
