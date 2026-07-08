from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"

    # URL de connexion du rôle app_user (NOBYPASSRLS, non propriétaire).
    # Pas de valeur par défaut : le secret vient de l'environnement.
    database_url: str

    # Clé de signature des JWT (HS256). Aucun défaut : absente = échec au
    # démarrage, jamais de clé de repli en dur dans le code.
    jwt_secret: str

    # Clé HMAC du chaînage du journal d'audit (étape 3b). Tenue hors base :
    # un attaquant qui altère audit_log sans la clé ne peut pas re-chaîner.
    audit_hmac_key: str

    # Clés Fernet du chiffrement des secrets TOTP au repos (2b), séparées
    # par des virgules : la PREMIÈRE chiffre, toutes déchiffrent
    # (MultiFernet). Rotation : nouvelle clé en tête, re-chiffrement par
    # migrator, retrait de l'ancienne. Aucun défaut.
    mfa_encryption_keys: str

    # Jeton intermédiaire d'authentification partielle (login à deux temps).
    mfa_token_ttl_seconds: int = 5 * 60
    # Vérification du second facteur : fenêtre par (IP, utilisateur)...
    mfa_rate_limit_attempts: int = 5
    mfa_rate_limit_window_seconds: int = 60
    # ...et fenêtre par utilisateur SEUL, indépendante de l'IP : un
    # brute-force distribué multi-IP ne contourne pas la limite. Temporaire
    # et auto-réinitialisée (TTL) : pas de verrouillage exploitable en déni
    # de service contre la victime.
    mfa_user_rate_limit_attempts: int = 10
    mfa_user_rate_limit_window_seconds: int = 300

    redis_url: str = "redis://localhost:6379/0"

    # Tokens courts + refresh long révocable (famille en Redis).
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 7 * 24 * 3600

    # Rate limiting du login : fenêtre fixe par (IP, email).
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 60

    # Outils containerisés (rendu PDF/A WeasyPrint, contrôle veraPDF).
    # Commandes pilotées par config : en CI ou une fois l'app containerisée,
    # seule la config change. Le répertoire d'échange est bind-mounté sur
    # /data dans les deux conteneurs.
    pdf_render_command: str = "docker compose run --rm -T pdf"
    verapdf_command: str = "docker compose run --rm -T verapdf"
    exchange_dir: str = ".exchange"

    # Connecteur Plateforme Agréée. Seule valeur admise pour l'instant :
    # le mock — aucune transmission réelle tant qu'une implémentation
    # sandbox n'est pas branchée volontairement ici.
    pa_connector: Literal["mock"] = "mock"

    # Worker de polling des statuts (5b).
    pa_poll_interval_seconds: int = 60
    pa_poll_retry_max: int = 5
    pa_poll_retry_intervals: list[int] = [10, 30, 60, 120, 300]

    # TODO(auth) : supprimer ce flag et la résolution par en-tête X-Tenant-Id
    # à l'étape auth — le tenant sera alors extrait du JWT vérifié.
    tenant_header_enabled: bool = False

    @model_validator(mode="after")
    def _tenant_header_reserve_au_dev(self) -> "Settings":
        if self.tenant_header_enabled and self.environment != "dev":
            raise ValueError(
                "APP_TENANT_HEADER_ENABLED n'est autorisé qu'en dev : "
                "l'en-tête X-Tenant-Id n'est pas authentifié."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
