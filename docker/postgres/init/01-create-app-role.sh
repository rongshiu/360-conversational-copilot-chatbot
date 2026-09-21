#!/usr/bin/env bash
# Create the application login role and databases.
#
# POSTGRES_USER is `postgres` (a superuser) and is used ONLY for provisioning.
# The application connects as a separate, deliberately unprivileged role:
#
#   NOSUPERUSER NOBYPASSRLS  -- a superuser is not subject to object privileges,
#                               and object privileges are the whole of access
#                               control: they are what stops an executive session
#                               reading ci_hod or the fact tables under it. A
#                               superuser login user would read both personas with
#                               no error at query time. This is the single most
#                               important property of the whole setup.
set -euo pipefail

APP_USER="${DB_USER:-app}"
APP_PASSWORD="${DB_PASSWORD:-app}"
APP_DB="${POSTGRES_DB:-customer_intelligence}"
CHECKPOINT_DB="${CHECKPOINT_DB_NAME:-copilot_checkpoint}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$APP_DB" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_USER}') THEN
        CREATE ROLE "${APP_USER}" LOGIN PASSWORD '${APP_PASSWORD}'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    ELSE
        ALTER ROLE "${APP_USER}" NOSUPERUSER NOBYPASSRLS;
    END IF;
END \$\$;

-- The entrypoint created this database owned by postgres; hand it to the app so
-- alembic can create schemas in it.
ALTER DATABASE "${APP_DB}" OWNER TO "${APP_USER}";
GRANT ALL ON SCHEMA public TO "${APP_USER}";

SELECT 'CREATE DATABASE "${CHECKPOINT_DB}" OWNER "${APP_USER}"'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${CHECKPOINT_DB}')\gexec
SQL

# LangGraph's checkpointer creates its own tables in public, so the app needs it.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$CHECKPOINT_DB" \
     -c "GRANT ALL ON SCHEMA public TO \"${APP_USER}\";"

echo "[init] created role ${APP_USER} (NOSUPERUSER NOBYPASSRLS) and databases ${APP_DB}, ${CHECKPOINT_DB}"
