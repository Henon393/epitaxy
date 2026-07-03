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
