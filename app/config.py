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
