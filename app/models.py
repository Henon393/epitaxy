import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
