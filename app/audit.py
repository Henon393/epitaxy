"""Service d'écriture du journal d'audit.

``record`` écrit dans la session de la requête : l'événement d'audit et la
mutation métier committent dans la même transaction. Si l'écriture d'audit
échoue, la mutation est annulée avec elle, fail-closed, pas de mutation
sans trace.

Les événements sans tenant résolu et existant (rate limit avant lookup,
login sur tenant inconnu) ne vont JAMAIS dans audit_log : la table est
strictement tenantée. Ils partent dans le logger applicatif ``app.security``,
télémétrie de sécurité côté opérateur, hors données tenant.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit_chain import (
    HASH_SCHEMA_VERSION,
    advance_chain_head,
    canonical_v1,
    compute_entry_hash,
    next_chain_link,
)
from app.db import get_current_tenant
from app.identity import get_current_user
from app.models import AuditAction, AuditLog

security_logger = logging.getLogger("app.security")

_UNSET: Any = object()


def record(
    session: Session,
    action: AuditAction,
    *,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    actor_id: uuid.UUID | None = _UNSET,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Écrit une ligne d'audit dans la transaction de ``session``.

    Par défaut, acteur et tenant sont lus depuis les ContextVars posés par
    le middleware. Les routes d'auth (exemptées de middleware) les passent
    explicitement.
    """
    if actor_id is _UNSET:
        user = get_current_user()
        actor_id = user.id if user is not None else None
    if tenant_id is None:
        tenant_id = get_current_tenant()

    # Chaînage 3b : la tête de chaîne du tenant est verrouillée jusqu'au
    # commit, ordre total sans fourche, le hash enregistre cet ordre.
    position, prev_hash = next_chain_link(session, tenant_id)
    entry_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    entry_hash = compute_entry_hash(
        canonical_v1(
            entry_id=entry_id,
            position=position,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=str(action),
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
            created_at=created_at,
        ),
        prev_hash,
    )
    session.add(
        AuditLog(
            id=entry_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=str(action),
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata,
            position=position,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            hash_schema_version=HASH_SCHEMA_VERSION,
            created_at=created_at,
        )
    )
    # Flush immédiat : un échec d'écriture d'audit doit faire échouer la
    # requête (et donc annuler la mutation métier), pas être découvert au
    # commit.
    session.flush()
    advance_chain_head(session, tenant_id, position, entry_hash)


def log_unattributed(action: AuditAction, **fields: Any) -> None:
    """Trace un événement de sécurité sans tenant résolu ou existant."""
    security_logger.warning(
        "evenement de securite sans tenant : action=%s %s",
        str(action),
        " ".join(f"{key}={value}" for key, value in sorted(fields.items())),
    )
