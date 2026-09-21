#!/usr/bin/env bash
# Create the two persona read roles (ci_copilot_hod, ci_copilot_exec), the write
# role (ci_loader), the legacy read role the personas replaced (ci_copilot) and the
# read-only human role (ci_analyst).
#
# Runs the same scripts/bootstrap_roles.sql the SETUP.md runbook uses, rather than
# duplicating the logic, so container and manual installs cannot drift.
#
# Needs superuser: CREATE ROLE and GRANT role-membership are cluster-level
# operations the application role deliberately cannot perform.
set -euo pipefail

APP_USER="${DB_USER:-app}"
APP_DB="${POSTGRES_DB:-customer_intelligence}"
COPILOT_HOD_ROLE="${CI_COPILOT_HOD_ROLE:-ci_copilot_hod}"
COPILOT_EXEC_ROLE="${CI_COPILOT_EXEC_ROLE:-ci_copilot_exec}"
COPILOT_ROLE="${CI_COPILOT_ROLE:-ci_copilot}"
LOADER_ROLE="${CI_LOADER_ROLE:-ci_loader}"
ANALYST_ROLE="${CI_ANALYST_ROLE:-ci_analyst}"
# Optional. Without it ci_analyst is created NOLOGIN -- the policies and grants
# exist, so enabling the account later is only a password away.
ANALYST_PASSWORD="${CI_ANALYST_PASSWORD:-}"

SQL_FILE=/opt/ci-scripts/bootstrap_roles.sql
if [ ! -f "$SQL_FILE" ]; then
  echo "[init] ERROR: $SQL_FILE not mounted. Add ./scripts:/opt/ci-scripts:ro to the postgres service." >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$APP_DB" \
     -v app_user="$APP_USER" \
     -v copilot_hod_role="$COPILOT_HOD_ROLE" \
     -v copilot_exec_role="$COPILOT_EXEC_ROLE" \
     -v copilot_role="$COPILOT_ROLE" \
     -v loader_role="$LOADER_ROLE" \
     -v analyst_role="$ANALYST_ROLE" \
     -v analyst_password="$ANALYST_PASSWORD" \
     -f "$SQL_FILE"

echo "[init] bootstrapped ${COPILOT_HOD_ROLE}, ${COPILOT_EXEC_ROLE}, ${COPILOT_ROLE} and ${LOADER_ROLE}; all granted to ${APP_USER} WITH INHERIT FALSE"
if [ -n "$ANALYST_PASSWORD" ]; then
  echo "[init] ${ANALYST_ROLE} enabled as a read-only login"
else
  echo "[init] ${ANALYST_ROLE} created NOLOGIN; set CI_ANALYST_PASSWORD to enable it"
fi
