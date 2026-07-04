"""Identité utilisateur de la requête courante et contrôle de rôle (RBAC).

Posée par le middleware après vérification du JWT, consommée par les
dépendances de rôle et, à l'étape suivante, par le journal d'audit.
"""

import uuid
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass

from fastapi import HTTPException

from app.models import Role


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


_current_user: ContextVar[CurrentUser | None] = ContextVar("current_user", default=None)


def set_current_user(user: CurrentUser) -> Token:
    return _current_user.set(user)


def reset_current_user(token: Token) -> None:
    _current_user.reset(token)


def get_current_user() -> CurrentUser | None:
    return _current_user.get()


def require_role(*roles: Role) -> Callable[[], CurrentUser]:
    """Dépendance FastAPI : 401 sans identité, 403 si le rôle ne suffit pas.

    Listes explicites, pas de hiérarchie implicite entre rôles.
    """
    allowed = {str(role) for role in roles}

    def dependency() -> CurrentUser:
        user = get_current_user()
        if user is None:
            raise HTTPException(status_code=401, detail="Authentification requise.")
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Rôle insuffisant.")
        return user

    return dependency
