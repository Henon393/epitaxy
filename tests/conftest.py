"""Fixtures communes : migrations, moteurs par rôle, jeu de données bi-tenant.

Les tests d'isolation tournent contre le vrai PostgreSQL du docker compose —
la RLS ne se teste pas sur SQLite. Trois connexions distinctes :
- superutilisateur : seed des données de test uniquement (bypasse la RLS) ;
- migrator : via Alembic pour appliquer le schéma ;
- app_user : la connexion testée, soumise à la RLS.
"""

import os
import uuid
from dataclasses import dataclass

import pytest
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import Connection, Engine, create_engine, text

from alembic import command

load_dotenv()


@pytest.fixture(scope="session")
def apply_migrations() -> None:
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(scope="session")
def super_engine(apply_migrations: None) -> Engine:
    return create_engine(os.environ["SUPER_DATABASE_URL"])


@pytest.fixture
def app_engine() -> Engine:
    # Pool réduit à UNE connexion : toute paire de requêtes successives
    # partage forcément la même connexion physique, ce qui rend le test de
    # non-fuite de contexte déterministe.
    engine = create_engine(os.environ["APP_DATABASE_URL"], pool_size=1, max_overflow=0)
    yield engine
    engine.dispose()


@dataclass
class SeedData:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    customer_a: uuid.UUID
    customer_b: uuid.UUID


@pytest.fixture
def seed(super_engine: Engine) -> SeedData:
    data = SeedData(
        tenant_a=uuid.uuid4(),
        tenant_b=uuid.uuid4(),
        customer_a=uuid.uuid4(),
        customer_b=uuid.uuid4(),
    )
    with super_engine.begin() as conn:
        conn.execute(text("TRUNCATE customers, tenants CASCADE"))
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:a, 'Tenant A'), (:b, 'Tenant B')"),
            {"a": data.tenant_a, "b": data.tenant_b},
        )
        conn.execute(
            text(
                "INSERT INTO customers (id, tenant_id, name, email) VALUES "
                "(:ca, :ta, 'Client A', 'a@exemple.fr'), "
                "(:cb, :tb, 'Client B', 'b@exemple.fr')"
            ),
            {
                "ca": data.customer_a,
                "ta": data.tenant_a,
                "cb": data.customer_b,
                "tb": data.tenant_b,
            },
        )
    return data


def set_tenant(conn: Connection, tenant_id: uuid.UUID) -> None:
    """Pose le contexte tenant pour la transaction en cours (SET LOCAL)."""
    conn.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
