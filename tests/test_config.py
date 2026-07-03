"""Tests du garde-fou de configuration sur l'en-tête X-Tenant-Id."""

import pytest
from pydantic import ValidationError

from app.config import Settings

DB_URL = "postgresql+psycopg://app_user:x@localhost:5432/facturation"


def test_tenant_header_refuse_hors_dev() -> None:
    with pytest.raises(ValidationError, match="dev"):
        Settings(
            _env_file=None,
            environment="prod",
            tenant_header_enabled=True,
            database_url=DB_URL,
        )


def test_tenant_header_accepte_en_dev() -> None:
    settings = Settings(
        _env_file=None,
        environment="dev",
        tenant_header_enabled=True,
        database_url=DB_URL,
    )
    assert settings.tenant_header_enabled is True
