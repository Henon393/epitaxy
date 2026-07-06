"""Soumission à la PA et cycle de vie synchrone des transmissions.

Le polling automatique et l'asynchrone viendront en 5b : ici, refresh
interroge le connecteur à la demande.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record
from app.db import get_db
from app.identity import require_role
from app.invoice_pdf import FACTURX_PDF_KIND
from app.models import (
    AuditAction,
    Invoice,
    InvoiceArtifact,
    InvoiceStatus,
    PaStatusEvent,
    PaTransmission,
    Role,
)
from app.pa import (
    InvalidTransitionError,
    PaStatus,
    StatusInfo,
    get_pa_connector,
    validate_transition,
)
from app.schemas import TransmissionOut

router = APIRouter(tags=["transmissions"])


def _last_transmission(db: Session, invoice_id: uuid.UUID) -> PaTransmission | None:
    return db.scalar(
        select(PaTransmission)
        .where(PaTransmission.invoice_id == invoice_id)
        .order_by(PaTransmission.created_at.desc())
        .limit(1)
    )


@router.post(
    "/invoices/{invoice_id}/submit",
    response_model=TransmissionOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def submit_invoice(
    invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> PaTransmission:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    if invoice.status != InvoiceStatus.emise:
        raise HTTPException(status_code=409, detail="Facture non émise : soumission refusée.")

    # On transmet le document normé, jamais autre chose.
    artifact = db.scalar(
        select(InvoiceArtifact).where(
            InvoiceArtifact.invoice_id == invoice_id,
            InvoiceArtifact.kind == FACTURX_PDF_KIND,
        )
    )
    if artifact is None:
        raise HTTPException(
            status_code=409,
            detail="Aucun artefact facturx_pdf : générer d'abord le Factur-X.",
        )

    previous = _last_transmission(db, invoice_id)
    if previous is not None:
        current = previous.current_status
        if current != PaStatus.rejetee:
            # refusee (refus commercial) appelle un avoir, hors périmètre ;
            # toute autre valeur = transmission en cours ou aboutie.
            raise HTTPException(
                status_code=409,
                detail=f"Transmission existante au statut {current!r} : re-soumission refusée.",
            )
        # Re-soumission après rejet : uniquement pour un rejet de TRANSPORT
        # (défaut d'acheminement ne touchant pas la facture) — on redépose
        # le MÊME artefact, inchangé.
        # TODO(contenu) : un rejet pour défaut de contenu impose la
        # réémission d'une NOUVELLE facture (nouveau numéro), la facture
        # étant immuable depuis 4a — hors périmètre, comme l'avoir.

    routing_identifier = invoice.buyer_siren  # TODO(SIRET) cf. PaTransmission
    result = get_pa_connector().submit(artifact.content, routing_identifier)

    transmission = PaTransmission(
        tenant_id=invoice.tenant_id,
        invoice_id=invoice.id,
        pa_transmission_ref=result.transmission_ref,
        routing_identifier=routing_identifier,
    )
    db.add(transmission)
    db.flush()
    db.add(
        PaStatusEvent(
            tenant_id=invoice.tenant_id,
            transmission_id=transmission.id,
            position=1,
            status=result.initial.status,
            reason=result.initial.reason,
        )
    )
    db.flush()
    record(
        db,
        AuditAction.invoice_submitted,
        target_type="pa_transmission",
        target_id=transmission.id,
        metadata={
            "invoice_id": str(invoice.id),
            "pa_transmission_ref": result.transmission_ref,
            "routing_identifier": routing_identifier,
        },
    )
    db.expire(transmission)
    return transmission


@router.post(
    "/transmissions/{transmission_id}/refresh",
    response_model=TransmissionOut,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def refresh_transmission(
    transmission_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> PaTransmission:
    transmission = db.get(PaTransmission, transmission_id)
    if transmission is None:
        raise HTTPException(status_code=404, detail="Transmission introuvable.")

    last_event = transmission.events[-1]
    current = PaStatus(last_event.status)
    info: StatusInfo = get_pa_connector().get_status(transmission.pa_transmission_ref)

    # Statut inchangé : rien à écrire — sauf encaissee, répétable par nature
    # (chaque paiement programmé produit son événement ; la déduplication de
    # polls identiques viendra avec les ids d'événements de la vraie PA, 5b).
    if info.status == current and info.status != PaStatus.encaissee:
        return transmission

    try:
        validate_transition(current, info.status)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if info.status == PaStatus.encaissee and (info.paid_amount is None or info.paid_at is None):
        raise HTTPException(
            status_code=422,
            detail="Statut encaissée : montant et date de paiement obligatoires.",
        )

    db.add(
        PaStatusEvent(
            tenant_id=transmission.tenant_id,
            transmission_id=transmission.id,
            position=last_event.position + 1,
            status=info.status,
            paid_amount=info.paid_amount,
            paid_at=info.paid_at,
            reason=info.reason,
        )
    )
    db.flush()
    record(
        db,
        AuditAction.transmission_status_changed,
        target_type="pa_transmission",
        target_id=transmission.id,
        metadata={
            "from": str(current),
            "to": str(info.status),
            **({"paid_amount": str(info.paid_amount)} if info.paid_amount is not None else {}),
            **({"paid_at": info.paid_at.isoformat()} if info.paid_at is not None else {}),
        },
    )
    db.expire(transmission)
    return transmission


@router.get("/invoices/{invoice_id}/transmission", response_model=TransmissionOut)
def get_transmission(
    invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> PaTransmission:
    transmission = _last_transmission(db, invoice_id)
    if transmission is None:
        raise HTTPException(status_code=404, detail="Aucune transmission pour cette facture.")
    return transmission
