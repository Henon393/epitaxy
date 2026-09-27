"""Middleware ASGI : authentification JWT et contexte tenant.

Le tenant est résolu depuis le JWT vérifié (signature HS256 imposée,
expiration, type access), qui pose aussi l'identité utilisateur pour le RBAC
et le futur journal d'audit. Hors chemins exemptés, pas de token valide =
401 avant toute exécution de route.
"""

import uuid

from redis.exceptions import RedisError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import reset_current_tenant, set_current_tenant
from app.identity import CurrentUser, reset_current_user, set_current_user
from app.security import TokenError, decode_token
from app.token_store import minimum_session_version

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
    "/auth/mfa/verify",
}

# Interface web (app/static) : la page et ses ressources sont des fichiers
# sans donnée métier, servis sans jeton — l'écran de connexion lui-même
# serait sinon inaccessible.
#
# Volontairement distinct d'EXEMPT_PATHS, qui reste la source de vérité
# des routes d'API ouvertes et sert au marquage OpenAPI : aucune route de
# données n'est exemptée ici. L'exemption porte sur des fichiers, pas sur
# des données, qui restent toutes derrière le contexte tenant et la RLS.
UI_PATHS = {"/", "/favicon.ico"}
UI_PREFIXES = ("/static/",)


def _est_interface(path: str) -> bool:
    return path in UI_PATHS or path.startswith(UI_PREFIXES)


class TenantContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] in EXEMPT_PATHS
            or _est_interface(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        authorization = headers.get("authorization")

        if authorization is not None:
            await self._handle_jwt(authorization, scope, receive, send)
            return

        # Sans jeton valide, aucun contexte tenant n'est posé : le JWT
        # vérifié est la seule voie d'entrée.
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

        # Révocation immédiate : la signature et la date d'expiration ne
        # disent rien d'une désactivation ou d'une rétrogradation survenue
        # depuis l'émission du jeton. Une lecture Redis, sans accès base.
        try:
            minimum = await minimum_session_version(claims["sub"])
        except RedisError:
            # Fail-closed : sans Redis on ne peut pas savoir si la session a
            # été révoquée. Refuser vaut mieux qu'accorder un accès peut-être
            # coupé. Cohérent avec le login, qui échoue déjà si Redis tombe.
            await self._indisponible(scope, receive, send)
            return
        if minimum is not None and int(claims["sv"]) < minimum:
            await self._reject(scope, receive, send, "Session révoquée, reconnectez-vous.")
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
    async def _indisponible(scope: Scope, receive: Receive, send: Send) -> None:
        """Impossible de vérifier l'état de la session : on refuse."""
        response = JSONResponse(
            {"detail": "Service temporairement indisponible."},
            status_code=503,
        )
        await response(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        response = JSONResponse(
            {"detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
