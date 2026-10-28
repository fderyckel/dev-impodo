#!/bin/sh
set -eu

runtime_password="$(tr -d '\r\n' < /run/impodo-postgres-secrets/postgres-runtime-password)"
migration_password="$(tr -d '\r\n' < /run/impodo-postgres-secrets/postgres-migration-password)"

if [ -z "${runtime_password}" ] || [ -z "${migration_password}" ]; then
    echo "Impodo PostgreSQL role provisioning refused: a password file is empty." >&2
    exit 1
fi

psql \
    --set=ON_ERROR_STOP=1 \
    --set=runtime_password="${runtime_password}" \
    --set=migration_password="${migration_password}" \
    --username "${POSTGRES_USER}" \
    --dbname "${POSTGRES_DB}" <<'SQL'
SELECT format(
    'CREATE ROLE impodo_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'impodo_runtime')
\gexec

SELECT format(
    'CREATE ROLE impodo_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migration_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'impodo_migrator')
\gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO impodo_runtime, impodo_migrator',
    current_database()
)
\gexec

SELECT format(
    'GRANT CREATE ON DATABASE %I TO impodo_migrator',
    current_database()
)
\gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
