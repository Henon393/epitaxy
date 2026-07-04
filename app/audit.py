"""Service d'écriture du journal d'audit.

``record`` écrit dans la session de la requête : l'événement d'audit et la
mutation métier committent dans la même transaction. Si l'écriture d'audit
échoue, la mutation est annulée avec elle — fail-closed, pas de mutation
sans trace.

Les événements sans tenant résolu et existant (rate limit avant lookup,
login sur tenant inconnu) ne vont JAMAIS dans audit_log : la table est
strictement tenantée. Ils partent dans le logger applicatif ``app.security``
— télémétrie de sécurité côté opérateur, hors données tenant.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

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

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=str(action),
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata,
        )
    )
    # Flush immédiat : un échec d'écriture d'audit doit faire échouer la
    # requête (et donc annuler la mutation métier), pas être découvert au
    # commit.
    session.flush()


def log_unattributed(action: AuditAction, **fields: Any) -> None:
    """Trace un événement de sécurité sans tenant résolu ou existant."""
    security_logger.warning(
        "evenement de securite sans tenant : action=%s %s",
        str(action),
        " ".join(f"{key}={value}" for key, value in sorted(fields.items())),
    )
