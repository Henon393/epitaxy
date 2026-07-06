import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(enum.StrEnum):
    admin = "admin"
    comptable = "comptable"
    lecture_seule = "lecture_seule"


class AuditAction(enum.StrEnum):
    login_succeeded = "login_succeeded"
    login_failed = "login_failed"
    # Uniquement dans le log applicatif (jamais en base : le tenant n'est pas
    # vérifié au moment où le rate limit frappe).
    login_rate_limited = "login_rate_limited"
    customer_created = "customer_created"
    customer_deleted = "customer_deleted"
    invoice_created = "invoice_created"
    invoice_issued = "invoice_issued"
    invoice_submitted = "invoice_submitted"
    transmission_status_changed = "transmission_status_changed"


class VatRegime(enum.StrEnum):
    reel_normal = "reel_normal"
    franchise_en_base = "franchise_en_base"


class InvoiceStatus(enum.StrEnum):
    brouillon = "brouillon"
    emise = "emise"


class OperationCategory(enum.StrEnum):
    livraison_de_biens = "livraison_de_biens"
    prestation_de_services = "prestation_de_services"
    mixte = "mixte"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    # Email unique PAR TENANT : le même email peut exister dans deux tenants,
    # d'où le login scoppé par tenant_id.
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    # Hash argon2 uniquement — jamais de mot de passe en clair.
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
    # TODO(2b) : chiffrer mfa_secret au repos (il donne le contrôle du second
    # facteur) avant toute activation du TOTP — ne jamais le stocker en clair.
    mfa_secret: Mapped[str | None] = mapped_column(String(255), default=None)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Journal d'audit append-only.

    Inaltérabilité runtime, deux barrières indépendantes : UPDATE/DELETE
    révoqués pour app_user (migration 0003), et RLS FORCE sans policy
    UPDATE/DELETE — l'absence de policy vaut refus.

    TODO(retention) : cette table contient des données personnelles (IP,
    email tenté dans metadata) et croît sans borne. Une purge par politique
    de rétention (RGPD art. 5-1-e, limitation de conservation) devra être
    exécutée par migrator — jamais par app_user, qui n'a ni UPDATE ni
    DELETE — pour respecter le RGPD sans sacrifier l'inaltérabilité runtime.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),
        # Filet anti-fourche du chaînage (3b) : l'ordre total par tenant est
        # acquis par le verrou de tête, l'unicité le garantit en dernier
        # ressort.
        UniqueConstraint("tenant_id", "position", name="uq_audit_log_tenant_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"))
    # Nullable : événements pré-authentification (login échoué sur email
    # inconnu). Pas de FK vers users : l'audit doit survivre à la
    # suppression du compte qui l'a produit.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    action: Mapped[str] = mapped_column(String(50))
    target_type: Mapped[str | None] = mapped_column(String(50), default=None)
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # « metadata » est réservé par SQLAlchemy (Base.metadata), d'où
    # l'attribut Python distinct pour la colonne du même nom.
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB, default=None)
    # Chaînage 3b. position : ordre total par tenant ; entry_hash =
    # HMAC-SHA256(clé, canonique ‖ prev_hash) ; hash_schema_version stockée
    # en clair ET incluse dans le canonique — le format peut évoluer sans
    # invalider la vérifiabilité des entrées antérieures.
    position: Mapped[int] = mapped_column(Integer)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
    hash_schema_version: Mapped[int] = mapped_column(Integer)
    # Posé côté application (record) : il entre dans le contenu haché, un
    # server_default serait inconnu au moment du calcul.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditChainHead(Base):
    """Tête de chaîne d'audit par tenant — un CURSEUR de sérialisation
    (verrou de ligne dans record()), jamais une source de vérité : la
    vérification recalcule tout depuis audit_log seul."""

    __tablename__ = "audit_chain_heads"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    last_hash: Mapped[str] = mapped_column(String(64))


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    # Nullables à la création, exigés à l'émission d'une facture (mentions
    # obligatoires côté acheteur : SIREN et adresse structurée EN 16931).
    siren: Mapped[str | None] = mapped_column(String(9), default=None)
    address_line1: Mapped[str | None] = mapped_column(String(200), default=None)
    address_line2: Mapped[str | None] = mapped_column(String(200), default=None)
    postal_code: Mapped[str | None] = mapped_column(String(10), default=None)
    city: Mapped[str | None] = mapped_column(String(100), default=None)
    country_code: Mapped[str | None] = mapped_column(String(2), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyProfile(Base):
    """Profil légal de l'émetteur (vendeur), 1-1 avec le tenant.

    Source des mentions vendeur figées dans le snapshot d'émission : après
    émission, une facture ne relit jamais ce profil.
    """

    __tablename__ = "company_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(200))
    siren: Mapped[str] = mapped_column(String(9))
    siret: Mapped[str | None] = mapped_column(String(14), default=None)
    address_line1: Mapped[str] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200), default=None)
    postal_code: Mapped[str] = mapped_column(String(10))
    city: Mapped[str] = mapped_column(String(100))
    country_code: Mapped[str] = mapped_column(String(2))
    # Requis au régime réel, optionnel en franchise en base (validation
    # portée par le schéma pydantic selon vat_regime).
    vat_number: Mapped[str | None] = mapped_column(String(20), default=None)
    legal_form: Mapped[str] = mapped_column(String(50))
    vat_regime: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Invoice(Base):
    """Facture. Brouillon librement modifiable ; une fois émise, l'en-tête
    (snapshot vendeur/acheteur compris), le numéro et les lignes sont figés
    par les triggers de la migration 0004 — la facture est autoportante."""

    __tablename__ = "invoices"
    __table_args__ = (
        # Filet anti-doublon indépendant de la logique applicative : unicité
        # du numéro par tenant, brouillons (number NULL) exclus.
        Index(
            "uq_invoices_tenant_number",
            "tenant_id",
            "number",
            unique=True,
            postgresql_where="number IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("customers.id"))
    # NULL = brouillon. Attribué à l'émission par le compteur verrouillé,
    # jamais réattribué (trigger).
    number: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(20), default=InvoiceStatus.brouillon)
    issue_date: Mapped[date | None] = mapped_column(Date, default=None)
    supply_date: Mapped[date | None] = mapped_column(Date, default=None)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)

    # Mentions de la réforme portées par la facture elle-même.
    operation_category: Mapped[str] = mapped_column(String(30))
    vat_on_debits: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_address: Mapped[str | None] = mapped_column(String(500), default=None)

    total_ht: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total_tva: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    total_ttc: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    vat_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)

    # --- Snapshot d'émission : NULL au brouillon, figé à l'émission. ---
    seller_legal_name: Mapped[str | None] = mapped_column(String(200), default=None)
    seller_siren: Mapped[str | None] = mapped_column(String(9), default=None)
    seller_siret: Mapped[str | None] = mapped_column(String(14), default=None)
    seller_address_line1: Mapped[str | None] = mapped_column(String(200), default=None)
    seller_address_line2: Mapped[str | None] = mapped_column(String(200), default=None)
    seller_postal_code: Mapped[str | None] = mapped_column(String(10), default=None)
    seller_city: Mapped[str | None] = mapped_column(String(100), default=None)
    seller_country_code: Mapped[str | None] = mapped_column(String(2), default=None)
    seller_vat_number: Mapped[str | None] = mapped_column(String(20), default=None)
    seller_legal_form: Mapped[str | None] = mapped_column(String(50), default=None)
    seller_vat_regime: Mapped[str | None] = mapped_column(String(30), default=None)
    # « TVA non applicable, art. 293 B du CGI » en franchise, NULL au réel.
    vat_mention: Mapped[str | None] = mapped_column(String(100), default=None)
    buyer_name: Mapped[str | None] = mapped_column(String(200), default=None)
    buyer_siren: Mapped[str | None] = mapped_column(String(9), default=None)
    buyer_address_line1: Mapped[str | None] = mapped_column(String(200), default=None)
    buyer_address_line2: Mapped[str | None] = mapped_column(String(200), default=None)
    buyer_postal_code: Mapped[str | None] = mapped_column(String(10), default=None)
    buyer_city: Mapped[str | None] = mapped_column(String(100), default=None)
    buyer_country_code: Mapped[str | None] = mapped_column(String(2), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["InvoiceLine"]] = relationship(
        cascade="all, delete-orphan",
        order_by="InvoiceLine.position",
        lazy="selectin",
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    designation: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_price_ht: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    # NULL = pas de taux applicable (franchise en base). Distinct d'un taux
    # zéro : Decimal("0") est un taux (régime réel) qui alimente la
    # ventilation ; None n'en alimente aucune. Toujours tester « is None ».
    vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), default=None)
    total_ht: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class InvoiceArtifact(Base):
    """Artefact généré d'une facture émise (XML CII en 4b-1, PDF/A-3 en 4b-2).

    Même régime d'inaltérabilité que audit_log : INSERT et SELECT seulement
    pour app_user (REVOKE UPDATE/DELETE + policies RLS par commande, migration
    0005). Le XML d'une facture émise ne se réécrit pas ; la régénération
    après correction d'un bug de mapping passe par migrator, comme la purge
    d'audit. La présence de l'artefact validé vaut « transmissible ».
    """

    __tablename__ = "invoice_artifacts"
    __table_args__ = (
        UniqueConstraint("invoice_id", "kind", name="uq_invoice_artifacts_invoice_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("invoices.id"))
    kind: Mapped[str] = mapped_column(String(20))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaTransmission(Base):
    """Transmission d'une facture émise vers la PA.

    Ligne immuable (append-only, migration 0006) : aucun champ de statut
    mutable — le statut courant est le dernier PaStatusEvent. La facture et
    sa transmission sont strictement séparées : une transmission rejetée ou
    refusée laisse la facture émise, numérotée et immuable (pas de
    libération de numéro, pas de trou).
    """

    __tablename__ = "pa_transmissions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("invoices.id"), index=True)
    pa_transmission_ref: Mapped[str] = mapped_column(String(64))
    # TODO(SIRET) : le vrai niveau de routage du dispositif est le SIRET de
    # l'établissement (annuaire) ; le SIREN de l'acheteur, mappé en 4b-1,
    # est un proxy acceptable pour le mock. À remplacer avec la vraie PA
    # (colonne buyer_siret + EAS 0009 côté CII à prévoir).
    routing_identifier: Mapped[str] = mapped_column(String(14))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    events: Mapped[list["PaStatusEvent"]] = relationship(
        order_by="PaStatusEvent.position", lazy="selectin"
    )

    @property
    def current_status(self) -> str | None:
        return self.events[-1].status if self.events else None


class PaStatusEvent(Base):
    """Événement de statut horodaté, append-only.

    L'unicité (transmission_id, position) sérialise les insertions
    concurrentes ; le trigger de la migration 0006 encode le même graphe de
    transitions que app/pa.py (ceinture et bretelles), positions contiguës
    comprises.
    """

    __tablename__ = "pa_status_events"
    __table_args__ = (
        UniqueConstraint("transmission_id", "position", name="uq_pa_status_events_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    transmission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pa_transmissions.id"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    # Obligatoires quand status = encaissee (schéma applicatif + trigger).
    paid_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)
    paid_at: Mapped[date | None] = mapped_column(Date, default=None)
    reason: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def status_code(self) -> str:
        from app.pa import PA_STATUS_CODES, PaStatus

        return PA_STATUS_CODES[PaStatus(self.status)]


class InvoiceCounter(Base):
    """Compteur de numérotation par tenant, incrémenté sous verrou de ligne
    dans la transaction d'émission (sans trou car transactionnel, sans
    doublon car verrouillé)."""

    __tablename__ = "invoice_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, default=0)
