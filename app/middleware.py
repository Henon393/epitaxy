"""Middleware ASGI qui pose le contexte tenant avant toute exécution de route.

Aucune requête applicative ne passe sans tenant : hors chemins exemptés
(santé, docs), l'absence de tenant vaut refus immédiat en 400.
"""

import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.db import reset_current_tenant, set_current_tenant

# Chemins sans données métier, seuls autorisés hors contexte tenant.
EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

TENANT_HEADER = b"x-tenant-id"


class TenantContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        tenant_id = self._resolve_tenant(scope)
        if tenant_id is None:
            response = JSONResponse({"detail": "Contexte tenant manquant."}, status_code=400)
            await response(scope, receive, send)
            return

        token = set_current_tenant(tenant_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(token)

    @staticmethod
    def _resolve_tenant(scope: Scope) -> uuid.UUID | None:
        # TODO(auth) : remplacer intégralement cette résolution par le claim
        # tenant du JWT vérifié. L'en-tête X-Tenant-Id n'est pas authentifié
        # et n'est toléré qu'en dev, derrière APP_TENANT_HEADER_ENABLED
        # (rejeté au démarrage hors dev, cf. app/config.py).
        if not get_settings().tenant_header_enabled:
            return None
        for name, value in scope["headers"]:
            if name == TENANT_HEADER:
                try:
                    return uuid.UUID(value.decode())
                except ValueError:
                    return None
        return None
