"""Accès aux données sous contexte tenant obligatoire.

Deux verrous complémentaires :
- applicatif : ``get_db`` refuse de fournir une session si aucun tenant n'a été
  posé dans le contexte de la requête ;
- base : les policies RLS s'appuient sur ``current_setting('app.tenant_id')``
  sans valeur par défaut, donc toute requête arrivée sans contexte échoue au
  lieu de retourner silencieusement des lignes.
"""

import uuid
from collections.abc import Iterator
from contextvars import ContextVar, Token

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)


class MissingTenantContextError(RuntimeError):
    """Levée quand une session est demandée hors de tout contexte tenant."""


def set_current_tenant(tenant_id: uuid.UUID) -> Token:
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant(token: Token) -> None:
    _current_tenant_id.reset(token)


def get_current_tenant() -> uuid.UUID:
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise MissingTenantContextError("Aucun tenant dans le contexte de la requête.")
    return tenant_id


engine = create_engine(get_settings().database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Fournit une session liée au tenant courant, une transaction par requête.

    ``set_config(..., is_local => true)`` est l'équivalent paramétrable de
    ``SET LOCAL`` : le contexte tenant vit dans la transaction et disparaît
    avec elle — rien ne persiste sur la connexion rendue au pool.
    """
    tenant_id = get_current_tenant()
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
