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
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text

from alembic import command

load_dotenv()


@pytest.fixture(autouse=True)
def clean_redis() -> None:
    """Redis dédié au projet : purge complète avant chaque test pour isoler
    rate limiting et familles de refresh."""
    from app.redis_client import get_redis

    get_redis().flushdb()


@pytest.fixture
def client(apply_migrations: None) -> TestClient:
    from app.main import app

    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup_tenant(
    client: TestClient,
    name: str = "Acme",
    email: str = "admin@exemple.fr",
    password: str = "mot-de-passe-solide",
) -> dict:
    response = client.post(
        "/auth/signup",
        json={"tenant_name": name, "email": email, "password": password},
    )
    assert response.status_code == 201, response.text
    return {**response.json(), "email": email, "password": password}


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
