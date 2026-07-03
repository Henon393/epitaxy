from logging.config import fileConfig

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine

from alembic import context
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


class MigrationSettings(BaseSettings):
    """Les migrations se connectent avec le rôle migrator (propriétaire des
    tables), jamais avec app_user ni le superutilisateur."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    migrator_database_url: str


def run_migrations_offline() -> None:
    context.configure(
        url=MigrationSettings().migrator_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(MigrationSettings().migrator_database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
