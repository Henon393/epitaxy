"""Chaîne le journal d'audit

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-06

Re-chaînage complet de l'existant, exécuté par migrator : les entrées déjà
présentes deviennent vérifiables (pas d'entrée d'ancrage qui laisserait
l'historique hors chaîne). Le re-chaînage établit une LIGNE DE BASE de
confiance à l'instant de la migration, il ne certifie pas rétroactivement
le passé (cf. docs/audit-chaine.md).

FORCE RLS s'applique aussi à migrator : une policy temporaire, créée et
supprimée dans la même transaction de migration, ouvre le SELECT/UPDATE le
temps du backfill. La RLS n'est ni désactivée ni contournée durablement.
"""

import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from pydantic_settings import BaseSettings, SettingsConfigDict

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class _Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    audit_hmac_key: str


# --- Copie GELÉE du schéma de hachage v1 (app/audit_chain.py) ---------------
# Une migration ne doit pas changer de comportement quand l'application
# évolue : pas d'import du code applicatif.

_HASH_SCHEMA_VERSION = 1


def _canonical_v1(row, position: int, created_at: datetime) -> str:
    metadata_canonical = (
        json.dumps(row.metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if row.metadata is not None
        else ""
    )
    return "\n".join(
        [
            str(_HASH_SCHEMA_VERSION),
            str(row.id),
            str(position),
            str(row.tenant_id),
            str(row.actor_id) if row.actor_id is not None else "",
            row.action,
            row.target_type or "",
            str(row.target_id) if row.target_id is not None else "",
            metadata_canonical,
            created_at.astimezone(UTC).isoformat(),
        ]
    )


def _genesis(key: bytes, tenant_id) -> str:
    return hmac.new(key, f"genesis:{tenant_id}".encode(), hashlib.sha256).hexdigest()


def _entry_hash(key: bytes, canonical: str, prev_hash: str) -> str:
    return hmac.new(
        key, canonical.encode("utf-8") + b"\n" + prev_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def upgrade() -> None:
    key = _Env().audit_hmac_key.encode("utf-8")
    bind = op.get_bind()

    # Tête de chaîne : curseur de sérialisation, verrouillé par record().
    op.create_table(
        "audit_chain_heads",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    # app_user a besoin d'INSERT/UPDATE (curseur), jamais de DELETE.
    op.execute("REVOKE DELETE ON audit_chain_heads FROM app_user")
    op.execute("ALTER TABLE audit_chain_heads ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_chain_heads FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON audit_chain_heads
        USING (tenant_id = current_setting('app.tenant_id')::uuid)
        """
    )

    # Colonnes de chaîne, nullables le temps du backfill.
    op.add_column("audit_log", sa.Column("position", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_log", sa.Column("entry_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_log", sa.Column("hash_schema_version", sa.Integer(), nullable=True))

    # Policy temporaire de migration : FORCE RLS s'applique à migrator, qui
    # n'a ni SELECT global ni UPDATE sur audit_log. Créée et supprimée dans
    # la même transaction.
    op.execute("CREATE POLICY migration_rechain ON audit_log FOR ALL USING (true)")

    tenants = bind.execute(
        sa.text("SELECT DISTINCT tenant_id FROM audit_log ORDER BY tenant_id")
    ).scalars()
    for tenant_id in tenants:
        rows = bind.execute(
            sa.text(
                "SELECT id, tenant_id, actor_id, action, target_type, target_id, metadata, "
                "created_at FROM audit_log WHERE tenant_id = :t "
                "ORDER BY created_at, id"  # ordre déterministe, id en départage
            ),
            {"t": tenant_id},
        ).all()
        prev_hash = _genesis(key, tenant_id)
        position = 0
        for row in rows:
            position += 1
            entry_hash = _entry_hash(key, _canonical_v1(row, position, row.created_at), prev_hash)
            bind.execute(
                sa.text(
                    "UPDATE audit_log SET position = :position, prev_hash = :prev_hash, "
                    "entry_hash = :entry_hash, hash_schema_version = :version WHERE id = :id"
                ),
                {
                    "position": position,
                    "prev_hash": prev_hash,
                    "entry_hash": entry_hash,
                    "version": _HASH_SCHEMA_VERSION,
                    "id": row.id,
                },
            )
            prev_hash = entry_hash
        bind.execute(
            sa.text(
                "INSERT INTO audit_chain_heads (tenant_id, position, last_hash) "
                "VALUES (:t, :position, :last_hash)"
            ),
            {"t": tenant_id, "position": position, "last_hash": prev_hash},
        )

    op.execute("DROP POLICY migration_rechain ON audit_log")

    op.alter_column("audit_log", "position", nullable=False)
    op.alter_column("audit_log", "prev_hash", nullable=False)
    op.alter_column("audit_log", "entry_hash", nullable=False)
    op.alter_column("audit_log", "hash_schema_version", nullable=False)
    op.create_unique_constraint(
        "uq_audit_log_tenant_position", "audit_log", ["tenant_id", "position"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_audit_log_tenant_position", "audit_log")
    for column in ("hash_schema_version", "entry_hash", "prev_hash", "position"):
        op.drop_column("audit_log", column)
    op.drop_table("audit_chain_heads")
