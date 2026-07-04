"""Routes factures : brouillons libres, émission transactionnelle avec
numérotation verrouillée et snapshot vendeur/acheteur figé."""

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.audit import record
from app.cii import CII_XML_KIND, CiiValidationError, generate_and_validate
from app.db import get_current_tenant, get_db
from app.identity import require_role
from app.invoice_pdf import (
    FACTURX_PDF_KIND,
    FacturxStructureError,
    assemble_facturx,
)
from app.invoicing import MENTION_293B, compute_totals, line_total_ht, next_invoice_number
from app.models import (
    AuditAction,
    CompanyProfile,
    Customer,
    Invoice,
    InvoiceArtifact,
    InvoiceLine,
    InvoiceStatus,
    Role,
    VatRegime,
)
from app.pdf_render import PdfRenderError
from app.schemas import InvoiceCreate, InvoiceLineIn, InvoiceOut
from app.verapdf import PdfAValidationError

router = APIRouter(tags=["invoices"])


def _build_lines(payload_lines: list[InvoiceLineIn], tenant_id: uuid.UUID) -> list[InvoiceLine]:
    return [
        InvoiceLine(
            tenant_id=tenant_id,
            position=position,
            designation=line.designation,
            quantity=line.quantity,
            unit_price_ht=line.unit_price_ht,
            vat_rate=line.vat_rate,
            total_ht=line_total_ht(line.quantity, line.unit_price_ht),
        )
        for position, line in enumerate(payload_lines)
    ]


def _apply_totals(invoice: Invoice, payload_lines: list[InvoiceLineIn]) -> None:
    totals = compute_totals(
        [(line.quantity, line.unit_price_ht, line.vat_rate) for line in payload_lines]
    )
    invoice.total_ht = totals.total_ht
    invoice.total_tva = totals.total_tva
    invoice.total_ttc = totals.total_ttc
    invoice.vat_breakdown = totals.breakdown


def _get_draft_or_409(db: Session, invoice_id: uuid.UUID) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    if invoice.status != InvoiceStatus.brouillon:
        raise HTTPException(status_code=409, detail="Facture émise : immuable.")
    return invoice


@router.post(
    "/invoices",
    response_model=InvoiceOut,
    status_code=201,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def create_invoice(payload: InvoiceCreate, db: Annotated[Session, Depends(get_db)]) -> Invoice:
    tenant_id = get_current_tenant()
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")

    invoice = Invoice(
        tenant_id=tenant_id,
        customer_id=payload.customer_id,
        operation_category=payload.operation_category,
        vat_on_debits=payload.vat_on_debits,
        delivery_address=payload.delivery_address,
        supply_date=payload.supply_date,
        due_date=payload.due_date,
        lines=_build_lines(payload.lines, tenant_id),
    )
    _apply_totals(invoice, payload.lines)
    db.add(invoice)
    db.flush()
    record(db, AuditAction.invoice_created, target_type="invoice", target_id=invoice.id)
    return invoice


@router.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(db: Annotated[Session, Depends(get_db)]) -> list[Invoice]:
    return list(db.scalars(select(Invoice).order_by(Invoice.created_at)))


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    return invoice


@router.put(
    "/invoices/{invoice_id}",
    response_model=InvoiceOut,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def update_invoice(
    invoice_id: uuid.UUID, payload: InvoiceCreate, db: Annotated[Session, Depends(get_db)]
) -> Invoice:
    invoice = _get_draft_or_409(db, invoice_id)
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=404, detail="Client introuvable.")

    invoice.customer_id = payload.customer_id
    invoice.operation_category = payload.operation_category
    invoice.vat_on_debits = payload.vat_on_debits
    invoice.delivery_address = payload.delivery_address
    invoice.supply_date = payload.supply_date
    invoice.due_date = payload.due_date
    # Remplacement complet des lignes (delete-orphan) — brouillon seulement.
    invoice.lines = _build_lines(payload.lines, invoice.tenant_id)
    _apply_totals(invoice, payload.lines)
    db.flush()
    return invoice


@router.delete(
    "/invoices/{invoice_id}",
    status_code=204,
    dependencies=[Depends(require_role(Role.admin))],
)
def delete_invoice(invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> None:
    # Un brouillon n'a pas de numéro : sa suppression ne crée jamais de trou.
    invoice = _get_draft_or_409(db, invoice_id)
    db.delete(invoice)
    db.flush()


def _validate_buyer(customer: Customer) -> None:
    manquants = [
        champ
        for champ in ("siren", "address_line1", "postal_code", "city", "country_code")
        if getattr(customer, champ) is None
    ]
    if manquants:
        raise HTTPException(
            status_code=422,
            detail=f"Client incomplet pour l'émission, champs manquants : {', '.join(manquants)}.",
        )


def _validate_regime_coherence(profile: CompanyProfile, invoice: Invoice) -> None:
    # Toujours « is None » : une ligne à 0 % est une ligne AVEC taux.
    if profile.vat_regime == VatRegime.franchise_en_base:
        if any(line.vat_rate is not None for line in invoice.lines):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Lignes avec taux de TVA incompatibles avec la franchise "
                    "en base (art. 293 B du CGI)."
                ),
            )
    else:
        if any(line.vat_rate is None for line in invoice.lines):
            raise HTTPException(
                status_code=422,
                detail="Ligne sans taux de TVA incompatible avec le régime réel normal.",
            )


@router.post(
    "/invoices/{invoice_id}/issue",
    response_model=InvoiceOut,
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def issue_invoice(invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> Invoice:
    tenant_id = get_current_tenant()

    # Étapes 1-4 : lectures et validations, AVANT le verrou compteur.
    invoice = _get_draft_or_409(db, invoice_id)
    if not invoice.lines:
        raise HTTPException(status_code=422, detail="Facture sans ligne : émission refusée.")

    customer = db.get(Customer, invoice.customer_id)
    _validate_buyer(customer)

    profile = db.get(CompanyProfile, tenant_id)
    if profile is None:
        raise HTTPException(
            status_code=422,
            detail="Profil entreprise (vendeur) non renseigné : émission refusée.",
        )
    _validate_regime_coherence(profile, invoice)

    # Recalcul défensif : jamais les totaux stockés sans revalidation.
    totals = compute_totals(
        [(line.quantity, line.unit_price_ht, line.vat_rate) for line in invoice.lines]
    )
    franchise = profile.vat_regime == VatRegime.franchise_en_base

    # Étape 5 : verrou compteur, pris le plus tard possible.
    number = next_invoice_number(db, tenant_id)

    # Étape 6 : bascule + snapshot en un seul UPDATE gardé par le statut.
    # rowcount 0 = émission concurrente de la même facture → 409 (et le
    # trigger reste la barrière ultime).
    result = db.execute(
        update(Invoice)
        .where(Invoice.id == invoice.id, Invoice.status == InvoiceStatus.brouillon)
        .values(
            status=InvoiceStatus.emise,
            number=number,
            issue_date=date.today(),
            total_ht=totals.total_ht,
            total_tva=totals.total_tva if not franchise else Decimal("0.00"),
            total_ttc=totals.total_ttc,
            vat_breakdown=totals.breakdown,
            vat_mention=MENTION_293B if franchise else None,
            seller_legal_name=profile.legal_name,
            seller_siren=profile.siren,
            seller_siret=profile.siret,
            seller_address_line1=profile.address_line1,
            seller_address_line2=profile.address_line2,
            seller_postal_code=profile.postal_code,
            seller_city=profile.city,
            seller_country_code=profile.country_code,
            seller_vat_number=profile.vat_number,
            seller_legal_form=profile.legal_form,
            seller_vat_regime=profile.vat_regime,
            buyer_name=customer.name,
            buyer_siren=customer.siren,
            buyer_address_line1=customer.address_line1,
            buyer_address_line2=customer.address_line2,
            buyer_postal_code=customer.postal_code,
            buyer_city=customer.city,
            buyer_country_code=customer.country_code,
        )
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Facture déjà émise.")

    # Étape 7 : audit dans la même transaction (fail-closed : un échec
    # annule l'émission et rend le numéro).
    record(db, AuditAction.invoice_issued, target_type="invoice", target_id=invoice.id)

    db.expire(invoice)
    return invoice


def _stored_artifact(
    db: Session, invoice_id: uuid.UUID, kind: str = CII_XML_KIND
) -> InvoiceArtifact | None:
    return db.scalar(
        select(InvoiceArtifact).where(
            InvoiceArtifact.invoice_id == invoice_id,
            InvoiceArtifact.kind == kind,
        )
    )


@router.post(
    "/invoices/{invoice_id}/cii",
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def generate_cii_artifact(
    invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> Response:
    """Génère (hors transaction d'émission), valide et stocke le XML CII.

    Idempotent : 201 à la création, 200 avec l'artefact existant ensuite —
    jamais de réécriture (table append-only).
    """
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    if invoice.status != InvoiceStatus.emise:
        raise HTTPException(status_code=409, detail="Facture non émise : pas de XML CII.")

    existing = _stored_artifact(db, invoice_id)
    if existing is not None:
        return Response(existing.content, status_code=200, media_type="application/xml")

    try:
        xml_bytes = generate_and_validate(invoice)
    except CiiValidationError as exc:
        # Fail-closed : rapport failed-assert remonté intact, aucun artefact.
        raise HTTPException(
            status_code=422,
            detail=f"Validation CII échouée, aucun artefact produit :\n{exc}",
        ) from exc

    db.add(
        InvoiceArtifact(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            kind=CII_XML_KIND,
            content=xml_bytes,
            sha256=hashlib.sha256(xml_bytes).hexdigest(),
        )
    )
    db.flush()
    return Response(xml_bytes, status_code=201, media_type="application/xml")


@router.get("/invoices/{invoice_id}/cii")
def get_cii_artifact(invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]) -> Response:
    artifact = _stored_artifact(db, invoice_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Aucun XML CII pour cette facture.")
    return Response(artifact.content, media_type="application/xml")


@router.post(
    "/invoices/{invoice_id}/facturx",
    dependencies=[Depends(require_role(Role.admin, Role.comptable))],
)
def generate_facturx_artifact(
    invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> Response:
    """Assemble le Factur-X final depuis l'artefact cii_xml stocké.

    Le XML n'est pas régénéré : sans artefact cii_xml, 409 explicite.
    Idempotent comme /cii : 201 à la création, 200 ensuite.
    """
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Facture introuvable.")
    if invoice.status != InvoiceStatus.emise:
        raise HTTPException(status_code=409, detail="Facture non émise : pas de Factur-X.")

    existing = _stored_artifact(db, invoice_id, FACTURX_PDF_KIND)
    if existing is not None:
        return Response(existing.content, status_code=200, media_type="application/pdf")

    cii_artifact = _stored_artifact(db, invoice_id, CII_XML_KIND)
    if cii_artifact is None:
        raise HTTPException(
            status_code=409,
            detail="Aucun artefact cii_xml : générer d'abord le XML (POST .../cii).",
        )

    try:
        pdf_bytes = assemble_facturx(invoice, cii_artifact.content)
    except (PdfRenderError, PdfAValidationError, FacturxStructureError) as exc:
        # Fail-closed : rapport intact, aucun artefact.
        raise HTTPException(
            status_code=422,
            detail=f"Assemblage Factur-X échoué, aucun artefact produit :\n{exc}",
        ) from exc

    db.add(
        InvoiceArtifact(
            tenant_id=invoice.tenant_id,
            invoice_id=invoice.id,
            kind=FACTURX_PDF_KIND,
            content=pdf_bytes,
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        )
    )
    db.flush()
    return Response(pdf_bytes, status_code=201, media_type="application/pdf")


@router.get("/invoices/{invoice_id}/facturx")
def get_facturx_artifact(
    invoice_id: uuid.UUID, db: Annotated[Session, Depends(get_db)]
) -> Response:
    artifact = _stored_artifact(db, invoice_id, FACTURX_PDF_KIND)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Aucun Factur-X pour cette facture.")
    return Response(artifact.content, media_type="application/pdf")
