#!/bin/sh
set -eu

postgres_uid="$(id -u postgres)"
postgres_gid="$(id -g postgres)"

install -d -o 0 -g 0 -m 0755 /run/impodo-postgres-init
install -o 0 -g 0 -m 0644 \
    /opt/impodo/010-impodo-roles.sh \
    /run/impodo-postgres-init/010-impodo-roles.sh

install -d -o "${postgres_uid}" -g "${postgres_gid}" -m 0750 \
    /run/impodo-postgres-secrets
install -o "${postgres_uid}" -g "${postgres_gid}" -m 0400 \
    /run/secrets/postgres-bootstrap-password \
    /run/impodo-postgres-secrets/postgres-bootstrap-password
install -o "${postgres_uid}" -g "${postgres_gid}" -m 0400 \
    /run/secrets/postgres-runtime-password \
    /run/impodo-postgres-secrets/postgres-runtime-password
install -o "${postgres_uid}" -g "${postgres_gid}" -m 0400 \
    /run/secrets/postgres-migration-password \
    /run/impodo-postgres-secrets/postgres-migration-password

install -d -o 0 -g 10001 -m 0750 /run/impodo-runtime-secrets
install -o 0 -g 10001 -m 0440 \
    /run/secrets/postgres-runtime-dsn \
    /run/impodo-runtime-secrets/postgres-runtime-dsn
install -o 0 -g 10001 -m 0440 \
    /run/secrets/platform-admin-password \
    /run/impodo-runtime-secrets/platform-admin-password
install -d -o 0 -g 10001 -m 0750 /run/impodo-migration-secrets
install -o 0 -g 10001 -m 0440 \
    /run/secrets/postgres-migration-dsn \
    /run/impodo-migration-secrets/postgres-migration-dsn

install -d -o 10001 -g 10001 -m 0750 \
    /var/lib/impodo/artifacts \
    /data \
    /config
