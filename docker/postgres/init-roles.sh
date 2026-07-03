#!/bin/bash
# Crée les deux rôles applicatifs à la première initialisation du cluster.
#
# Séparation des privilèges, clé de voûte de la RLS :
#  - migrator : propriétaire des tables (Alembic uniquement). PostgreSQL
#    n'applique pas la RLS au propriétaire sans FORCE ROW LEVEL SECURITY,
#    d'où le FORCE posé dans les migrations en plus de ce rôle dédié.
#  - app_user : rôle de l'application, non propriétaire et NOBYPASSRLS.
#    La RLS s'applique donc à toutes ses requêtes, sans exception.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE migrator LOGIN PASSWORD '${POSTGRES_MIGRATOR_PASSWORD}'
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    CREATE ROLE app_user LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}'
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

    GRANT CREATE, USAGE ON SCHEMA public TO migrator;
    GRANT USAGE ON SCHEMA public TO app_user;

    -- Toute table créée par migrator (migrations futures comprises) donne
    -- automatiquement le DML à app_user : impossible d'oublier un GRANT
    -- dans une migration.
    ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
    ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO app_user;
EOSQL
