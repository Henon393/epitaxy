"""Middleware ASGI : authentification JWT et contexte tenant.

Le tenant est résolu depuis le JWT vérifié (signature HS256 imposée,
expiration, type access), qui pose aussi l'identité utilisateur pour le RBAC
et le futur journal d'audit. Hors chemins exemptés, pas de token valide =
401 avant toute exécution de route.
"""

import uuid

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.db import reset_current_tenant, set_current_tenant
from app.identity import CurrentUser, reset_current_user, set_current_user
from app.security import TokenError, decode_token

# Chemins sans données métier ni identité préalable : santé, docs, et les
# routes d'auth qui gèrent elles-mêmes leur contexte (signup/login/refresh).
EXEMPT_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/signup",
    "/auth/login",
    "/auth/refresh",
}


class TenantContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        authorization = headers.get("authorization")

        if authorization is not None:
            await self._handle_jwt(authorization, scope, receive, send)
            return

        # Sans JWT : seul repli toléré, l'en-tête X-Tenant-Id en dev derrière
        # APP_TENANT_HEADER_ENABLED (rejeté au démarrage hors dev, cf.
        # app/config.py). Il pose un tenant sans identité utilisateur : les
        # routes protégées par rôle restent inaccessibles par ce chemin.
        if get_settings().tenant_header_enabled:
            tenant_id = self._tenant_from_header(headers)
            if tenant_id is not None:
                token = set_current_tenant(tenant_id)
                try:
                    await self.app(scope, receive, send)
                finally:
                    reset_current_tenant(token)
                return

        await self._reject(scope, receive, send, "Authentification requise.")

    async def _handle_jwt(
        self, authorization: str, scope: Scope, receive: Receive, send: Send
    ) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            await self._reject(scope, receive, send, "Schéma d'autorisation invalide.")
            return
        try:
            claims = decode_token(token.strip(), expected_type="access")
        except TokenError:
            await self._reject(scope, receive, send, "Token invalide ou expiré.")
            return

        user = CurrentUser(
            id=uuid.UUID(claims["sub"]),
            tenant_id=uuid.UUID(claims["tenant_id"]),
            role=claims["role"],
        )
        tenant_token = set_current_tenant(user.tenant_id)
        user_token = set_current_user(user)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_user(user_token)
            reset_current_tenant(tenant_token)

    @staticmethod
    def _tenant_from_header(headers: Headers) -> uuid.UUID | None:
        raw = headers.get("x-tenant-id")
        if raw is None:
            return None
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        response = JSONResponse(
            {"detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
