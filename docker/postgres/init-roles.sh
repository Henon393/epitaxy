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

    -- pa_scanner : rôle NOLOGIN du worker de polling (étape 5b). Propriétaire
    -- de la fonction SECURITY DEFINER de découverte des transmissions
    -- actives ; il reçoit des policies RLS ciblées en lecture seule dans la
    -- migration 0008. GRANT à migrator pour que les migrations puissent lui
    -- transférer la propriété de la fonction.
    CREATE ROLE pa_scanner NOLOGIN NOBYPASSRLS;
    GRANT pa_scanner TO migrator;
    -- USAGE : exécution des fonctions SECURITY DEFINER dont il est
    -- propriétaire. CREATE : exigé par ALTER FUNCTION ... OWNER TO dans les
    -- migrations (seul le superuser peut l'accorder ; NOLOGIN, le rôle ne
    -- s'exerce qu'à travers les fonctions definer écrites par les migrations).
    GRANT USAGE, CREATE ON SCHEMA public TO pa_scanner;

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
