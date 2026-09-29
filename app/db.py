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
from contextlib import contextmanager
from contextvars import ContextVar, Token

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

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


@contextmanager
def session_for_tenant(tenant_id: uuid.UUID) -> Iterator[Session]:
    """Session liée à un tenant explicite, une transaction par usage.

    ``set_config(..., is_local => true)`` est l'équivalent paramétrable de
    ``SET LOCAL`` : le contexte tenant vit dans la transaction et disparaît
    avec elle, rien ne persiste sur la connexion rendue au pool.

    Utilisé directement par signup/login, où le tenant vient de la
    revendication du client : il ne sert que de périmètre de recherche RLS,
    c'est argon2 qui authentifie.
    """
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        try:
            yield session
            # Filet, et non chemin nominal pour les routes de l'API.
            #
            # Cette sortie de contexte s'exécute APRÈS l'envoi de la réponse :
            # un commit posé ici laisserait une fenêtre pendant laquelle le
            # client, déjà informé du succès, enchaînerait une requête qui ne
            # verrait pas encore l'écriture. Les routes de l'API valident donc
            # avant de répondre (voir app/routing.py), ce qui rend ce commit
            # sans objet : `in_transaction()` est alors faux.
            #
            # Il reste utile aux appels hors requête HTTP, comme le worker, et
            # aux routes qui ouvrent leur session elles-mêmes.
            if session.in_transaction():
                session.commit()
        except Exception:
            session.rollback()
            raise


# Clé de dépôt de la session dans le scope ASGI de la requête.
#
# Le scope est un dictionnaire partagé, lisible depuis n'importe quel fil.
# Une ContextVar ne conviendrait pas : les routes synchrones et les
# dépendances à `yield` synchrones s'exécutent dans un fil du pool, dont le
# contexte ne remonte pas vers l'appelant qui doit valider la transaction.
CLE_SESSION = "epitaxy.session"


def get_db(request: Request) -> Iterator[Session]:
    """Fournit une session liée au tenant courant (posé par le middleware).

    La session est déposée dans le scope de la requête pour que la classe de
    route puisse valider la transaction avant que la réponse ne parte.
    """
    with session_for_tenant(get_current_tenant()) as session:
        request.scope[CLE_SESSION] = session
        try:
            yield session
        finally:
            request.scope.pop(CLE_SESSION, None)


def session_de_la_requete(scope: dict) -> Session | None:
    """Session ouverte pour cette requête, s'il y en a une."""
    return scope.get(CLE_SESSION)
