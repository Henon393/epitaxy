"""Chaînage cryptographique du journal d'audit (tamper-evidence).

Chaque entrée porte le HMAC-SHA256 de son contenu canonique concaténé au
hash de l'entrée précédente ; la première se chaîne sur un genesis lié au
tenant. Le chaînage COMPLÈTE le contrôle d'accès (REVOKE + RLS) : le
contrôle d'accès empêche, le chaînage détecte.

Anti-fourche : la tête de chaîne (audit_chain_heads) est verrouillée par
ligne, façon compteur de factures 4a — deux écritures concurrentes du même
tenant se sérialisent avant tout calcul de hash, l'ordre total est acquis
par exclusion mutuelle. Filet : UNIQUE (tenant_id, position) sur audit_log.

Portée exacte des garanties : docs/audit-chaine.md.
"""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditLog

# Version du schéma de hachage, stockée en clair sur chaque entrée ET
# incluse dans le contenu canonique : le format canonique peut évoluer sans
# invalider la vérifiabilité des entrées antérieures — la vérification lit
# la version pour appliquer le bon format.
HASH_SCHEMA_VERSION = 1


def _key() -> bytes:
    return get_settings().audit_hmac_key.encode("utf-8")


def _canonical_datetime(value: datetime) -> str:
    # Représentation unique quelle que soit la timezone de session du
    # driver : toujours UTC, ISO-8601.
    return value.astimezone(UTC).isoformat()


def canonical_v1(
    *,
    entry_id: uuid.UUID,
    position: int,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str | None,
    target_id: uuid.UUID | None,
    metadata: dict | None,
    created_at: datetime,
) -> str:
    """Sérialisation canonique v1 : champs en ordre fixe, joints par \\n,
    JSON des métadonnées trié et compact — même exigence de reproductibilité
    que le XML de 4b. Toute évolution passe par une nouvelle version, jamais
    par la modification de celle-ci (elle est gelée, y compris dans la
    migration 0007)."""
    metadata_canonical = (
        json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if metadata is not None
        else ""
    )
    return "\n".join(
        [
            str(HASH_SCHEMA_VERSION),
            str(entry_id),
            str(position),
            str(tenant_id),
            str(actor_id) if actor_id is not None else "",
            action,
            target_type or "",
            str(target_id) if target_id is not None else "",
            metadata_canonical,
            _canonical_datetime(created_at),
        ]
    )


def genesis_hash(tenant_id: uuid.UUID) -> str:
    """Ancre initiale, liée au tenant : une chaîne ne se transplante pas."""
    return hmac.new(_key(), f"genesis:{tenant_id}".encode(), hashlib.sha256).hexdigest()


def compute_entry_hash(canonical: str, prev_hash: str) -> str:
    return hmac.new(
        _key(), canonical.encode("utf-8") + b"\n" + prev_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def next_chain_link(session: Session, tenant_id: uuid.UUID) -> tuple[int, str]:
    """Position suivante et hash précédent, sous VERROU de la tête de chaîne.

    L'UPDATE no-op de l'UPSERT prend le verrou de ligne : toute écriture
    concurrente du même tenant attend le commit/rollback — aucune fourche
    possible, le hash ne fait qu'enregistrer un ordre déjà acquis. Verrou
    tenu jusqu'au commit : les écritures d'audit d'un tenant se sérialisent
    (même trade-off assumé que la numérotation sans trou). record() est
    toujours le DERNIER verrou pris (après le compteur de factures le cas
    échéant) : ordre uniforme, pas de deadlock.
    """
    row = session.execute(
        text(
            "INSERT INTO audit_chain_heads (tenant_id, position, last_hash) "
            "VALUES (:tenant_id, 0, :genesis) "
            "ON CONFLICT (tenant_id) DO UPDATE SET position = audit_chain_heads.position "
            "RETURNING position, last_hash"
        ),
        {"tenant_id": str(tenant_id), "genesis": genesis_hash(tenant_id)},
    ).one()
    return row.position + 1, row.last_hash


def advance_chain_head(
    session: Session, tenant_id: uuid.UUID, position: int, entry_hash: str
) -> None:
    session.execute(
        text(
            "UPDATE audit_chain_heads SET position = :position, last_hash = :entry_hash "
            "WHERE tenant_id = :tenant_id"
        ),
        {"position": position, "entry_hash": entry_hash, "tenant_id": str(tenant_id)},
    )


@dataclass(frozen=True)
class ChainVerification:
    intact: bool
    entries: int
    broken_at_position: int | None = None
    reason: str | None = None


def verify_chain(session: Session, tenant_id: uuid.UUID) -> ChainVerification:
    """Recalcule la chaîne du tenant depuis audit_log SEUL (jamais depuis la
    tête, qui n'est qu'un curseur) et rend le point exact de rupture."""
    rows = list(
        session.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.position)
        )
    )
    prev_hash = genesis_hash(tenant_id)
    for index, row in enumerate(rows, start=1):
        if row.position != index:
            return ChainVerification(
                intact=False,
                entries=len(rows),
                broken_at_position=index,
                reason=f"position non contiguë : {row.position} au lieu de {index} "
                "(entrée supprimée ou insérée)",
            )
        if row.prev_hash != prev_hash:
            return ChainVerification(
                intact=False,
                entries=len(rows),
                broken_at_position=index,
                reason="prev_hash incohérent avec l'entrée précédente",
            )
        if row.hash_schema_version != HASH_SCHEMA_VERSION:
            return ChainVerification(
                intact=False,
                entries=len(rows),
                broken_at_position=index,
                reason=f"version de schéma de hachage inconnue : {row.hash_schema_version}",
            )
        recomputed = compute_entry_hash(
            canonical_v1(
                entry_id=row.id,
                position=row.position,
                tenant_id=row.tenant_id,
                actor_id=row.actor_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                metadata=row.metadata_json,
                created_at=row.created_at,
            ),
            prev_hash,
        )
        if recomputed != row.entry_hash:
            return ChainVerification(
                intact=False,
                entries=len(rows),
                broken_at_position=index,
                reason="entry_hash invalide : contenu altéré ou re-chaîné sans la clé",
            )
        prev_hash = recomputed

    # Croisement avec la tête (curseur applicatif, non fiable en soi) : une
    # tête en avance est un indice de troncature de fin de chaîne — avec la
    # limite documentée : tête et chaîne remises en arrière ensemble sont
    # indétectables sans ancrage externe.
    head = session.execute(
        text("SELECT position FROM audit_chain_heads WHERE tenant_id = :t"),
        {"t": str(tenant_id)},
    ).one_or_none()
    if head is not None and head.position != len(rows):
        return ChainVerification(
            intact=False,
            entries=len(rows),
            broken_at_position=len(rows),
            reason=f"tête de chaîne incohérente : position {head.position} "
            f"pour {len(rows)} entrées (troncature possible)",
        )
    return ChainVerification(intact=True, entries=len(rows))
