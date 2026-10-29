"""Apply forward-only PostgreSQL control-schema migrations atomically."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, LiteralString, Protocol

import psycopg

from impodo.adapters.postgresql.control_schema import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
    POSTGRESQL_MIGRATION_ROLE,
    POSTGRESQL_RUNTIME_ROLE,
)
from impodo.adapters.postgresql.startup import (
    PostgreSQLDsnError,
    PostgreSQLDsnReader,
)

POSTGRESQL_MIGRATION_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_MIGRATION_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_MIGRATION_STATEMENT_TIMEOUT_MILLISECONDS = 60_000

_MIGRATION_LOCK_NAMESPACE = 1_229_803_599
_MIGRATION_LOCK_RESOURCE = 1_146_047_815
_LOCK_QUERY: LiteralString = "SELECT pg_advisory_xact_lock(%s, %s)"
_CREATE_SCHEMA_QUERY: LiteralString = "CREATE SCHEMA IF NOT EXISTS impodo_control"
_CREATE_LEDGER_QUERY: LiteralString = (
    "CREATE TABLE IF NOT EXISTS impodo_control.schema_migration (\n"
    "    version integer PRIMARY KEY CHECK (version > 0),\n"
    "    name text NOT NULL CHECK (name <> ''),\n"
    "    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),\n"
    "    applied_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp()\n"
    ")"
)
_READ_LEDGER_QUERY: LiteralString = (
    "SELECT version, name, checksum\n"
    "FROM impodo_control.schema_migration\n"
    "ORDER BY version"
)
_INSERT_LEDGER_QUERY: LiteralString = (
    "INSERT INTO impodo_control.schema_migration (version, name, checksum)\n"
    "VALUES (%s, %s, %s)"
)
_READ_CONTRACT_QUERY: LiteralString = (
    "SELECT generation, version\n"
    "FROM impodo_control.schema_contract\n"
    "WHERE contract_name = %s\n"
    "ORDER BY contract_name\n"
    "LIMIT 2"
)

_CREATE_CONTRACT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.schema_contract (\n"
    "    contract_name text PRIMARY KEY CHECK (contract_name <> ''),\n"
    "    generation text NOT NULL CHECK (generation <> ''),\n"
    "    version integer NOT NULL CHECK (version > 0),\n"
    "    updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp()\n"
    ")"
)
_INSERT_CONTRACT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.schema_contract "
    "(contract_name, generation, version)\n"
    "VALUES (%s, %s, %s)"
)
_GRANT_SCHEMA_QUERY: LiteralString = (
    "GRANT USAGE ON SCHEMA impodo_control TO impodo_runtime"
)
_GRANT_CONTRACT_QUERY: LiteralString = (
    "GRANT SELECT ON impodo_control.schema_contract TO impodo_runtime"
)
_CREATE_ORGANIZATION_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.organization (\n"
    "    organization_id varchar(200) PRIMARY KEY "
    "CHECK (organization_id <> '' AND organization_id = btrim(organization_id)),\n"
    "    display_name varchar(200) NOT NULL "
    "CHECK (display_name <> '' AND display_name = btrim(display_name)),\n"
    "    status varchar(32) NOT NULL "
    "CHECK (status IN ('ACTIVE', 'SUSPENDED')),\n"
    "    policy_revision bigint NOT NULL CHECK (policy_revision > 0),\n"
    "    optimistic_revision bigint NOT NULL CHECK (optimistic_revision > 0),\n"
    "    deletion_state varchar(32) NOT NULL "
    "CHECK (deletion_state IN ('NONE', 'REQUESTED', 'DELETING', 'DELETED')),\n"
    "    created_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    suspended_at timestamp with time zone,\n"
    "    deletion_requested_at timestamp with time zone,\n"
    "    deleted_at timestamp with time zone,\n"
    "    CHECK ((status = 'ACTIVE' AND suspended_at IS NULL) "
    "OR (status = 'SUSPENDED' AND suspended_at IS NOT NULL)),\n"
    "    CHECK ((deletion_state = 'NONE' "
    "AND deletion_requested_at IS NULL AND deleted_at IS NULL) "
    "OR (deletion_state IN ('REQUESTED', 'DELETING') "
    "AND deletion_requested_at IS NOT NULL AND deleted_at IS NULL) "
    "OR (deletion_state = 'DELETED' "
    "AND deletion_requested_at IS NOT NULL AND deleted_at IS NOT NULL))\n"
    ")"
)
_CREATE_PROJECT_CATALOGUE_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.project_catalogue (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    project_id uuid NOT NULL,\n"
    "    display_name varchar(200) NOT NULL "
    "CHECK (display_name <> '' AND display_name = btrim(display_name)),\n"
    "    status varchar(32) NOT NULL "
    "CHECK (status IN ('DRAFT', 'ACTIVE', 'CLOSED', 'ARCHIVED')),\n"
    "    data_classification varchar(32) NOT NULL "
    "CHECK (data_classification IN "
    "('INTERNAL', 'CONFIDENTIAL', 'RESTRICTED')),\n"
    "    retention_days smallint NOT NULL "
    "CHECK (retention_days BETWEEN 1 AND 3650),\n"
    "    optimistic_revision bigint NOT NULL CHECK (optimistic_revision > 0),\n"
    "    catalogue_state varchar(32) NOT NULL "
    "CHECK (catalogue_state IN "
    "('PENDING', 'READY', 'REPAIR_REQUIRED', 'DELETING', 'DELETED')),\n"
    "    artifact_root_key varchar(500),\n"
    "    current_receipt_hash varchar(71) "
    "CHECK (current_receipt_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    created_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    deletion_requested_at timestamp with time zone,\n"
    "    deleted_at timestamp with time zone,\n"
    "    PRIMARY KEY (organization_id, project_id),\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    CHECK ((catalogue_state = 'DELETED' AND deleted_at IS NOT NULL) "
    "OR (catalogue_state <> 'DELETED' AND deleted_at IS NULL)),\n"
    "    CHECK ((catalogue_state IN ('DELETING', 'DELETED') "
    "AND deletion_requested_at IS NOT NULL) "
    "OR (catalogue_state NOT IN ('DELETING', 'DELETED') "
    "AND deletion_requested_at IS NULL)),\n"
    "    CHECK (catalogue_state <> 'READY' "
    "OR (artifact_root_key IS NOT NULL AND current_receipt_hash IS NOT NULL))\n"
    ")"
)
_CREATE_PROJECT_LIST_INDEX_QUERY: LiteralString = (
    "CREATE INDEX project_catalogue_organization_status_idx\n"
    "ON impodo_control.project_catalogue "
    "(organization_id, status, updated_at DESC, project_id)"
)
_CREATE_PROJECT_REPAIR_INDEX_QUERY: LiteralString = (
    "CREATE INDEX project_catalogue_organization_repair_idx\n"
    "ON impodo_control.project_catalogue "
    "(organization_id, catalogue_state, updated_at, project_id)"
)
_GRANT_ORGANIZATION_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.organization TO impodo_runtime"
)
_GRANT_PROJECT_CATALOGUE_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.project_catalogue TO impodo_runtime"
)
_CREATE_OPERATION_REQUEST_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.operation_request (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    operation_id uuid NOT NULL,\n"
    "    project_id uuid NOT NULL,\n"
    "    operation_kind varchar(300) NOT NULL "
    "CHECK (operation_kind <> '' AND operation_kind = btrim(operation_kind)),\n"
    "    idempotency_key varchar(300) NOT NULL "
    "CHECK (idempotency_key <> '' AND idempotency_key = btrim(idempotency_key)),\n"
    "    semantic_input_hash varchar(71) NOT NULL "
    "CHECK (semantic_input_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    authorization_receipt_id varchar(300) NOT NULL "
    "CHECK (authorization_receipt_id <> '' "
    "AND authorization_receipt_id = btrim(authorization_receipt_id)),\n"
    "    requested_by_issuer varchar(500) NOT NULL "
    "CHECK (requested_by_issuer <> '' "
    "AND requested_by_issuer = btrim(requested_by_issuer)),\n"
    "    requested_by_subject varchar(500) NOT NULL "
    "CHECK (requested_by_subject <> '' "
    "AND requested_by_subject = btrim(requested_by_subject)),\n"
    "    requested_by_display_name varchar(200) NOT NULL "
    "CHECK (requested_by_display_name <> '' "
    "AND requested_by_display_name = btrim(requested_by_display_name)),\n"
    "    state varchar(32) NOT NULL DEFAULT 'RESERVED' "
    "CHECK (state IN "
    "('RESERVED', 'DATA_COMMITTED', 'PROJECTED', 'INTERRUPTED', "
    "'MANUAL_REVIEW')),\n"
    "    data_plane_receipt_hash varchar(71) "
    "CHECK (data_plane_receipt_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    requested_at timestamp with time zone NOT NULL,\n"
    "    updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    interrupted_at timestamp with time zone,\n"
    "    recovery_disposition varchar(1000),\n"
    "    PRIMARY KEY (organization_id, operation_id),\n"
    "    UNIQUE (organization_id, idempotency_key),\n"
    "    FOREIGN KEY (organization_id, project_id) "
    "REFERENCES impodo_control.project_catalogue "
    "(organization_id, project_id) ON DELETE RESTRICT,\n"
    "    CHECK ((state = 'RESERVED' AND data_plane_receipt_hash IS NULL) "
    "OR (state <> 'RESERVED' AND data_plane_receipt_hash IS NOT NULL) "
    "OR (state IN ('INTERRUPTED', 'MANUAL_REVIEW'))),\n"
    "    CHECK ((state IN ('INTERRUPTED', 'MANUAL_REVIEW') "
    "AND interrupted_at IS NOT NULL AND recovery_disposition IS NOT NULL) "
    "OR (state NOT IN ('INTERRUPTED', 'MANUAL_REVIEW') "
    "AND interrupted_at IS NULL AND recovery_disposition IS NULL))\n"
    ")"
)
_CREATE_OPERATION_PROJECT_INDEX_QUERY: LiteralString = (
    "CREATE INDEX operation_request_organization_project_state_idx\n"
    "ON impodo_control.operation_request "
    "(organization_id, project_id, state, requested_at, operation_id)"
)
_CREATE_DATA_PLANE_RECEIPT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.data_plane_receipt (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    operation_id uuid NOT NULL,\n"
    "    receipt_id varchar(300) NOT NULL "
    "CHECK (receipt_id <> '' AND receipt_id = btrim(receipt_id)),\n"
    "    project_id uuid NOT NULL,\n"
    "    workspace_id uuid,\n"
    "    operation_kind varchar(300) NOT NULL "
    "CHECK (operation_kind <> '' AND operation_kind = btrim(operation_kind)),\n"
    "    semantic_input_hash varchar(71) NOT NULL "
    "CHECK (semantic_input_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    fencing_token bigint CHECK (fencing_token > 0),\n"
    "    evidence_hash varchar(71) NOT NULL "
    "CHECK (evidence_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    receipt_hash varchar(71) NOT NULL "
    "CHECK (receipt_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    artifacts jsonb NOT NULL CHECK (jsonb_typeof(artifacts) = 'array'),\n"
    "    committed_at timestamp with time zone NOT NULL,\n"
    "    PRIMARY KEY (organization_id, operation_id),\n"
    "    UNIQUE (organization_id, receipt_id),\n"
    "    FOREIGN KEY (organization_id, operation_id) "
    "REFERENCES impodo_control.operation_request "
    "(organization_id, operation_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (organization_id, project_id) "
    "REFERENCES impodo_control.project_catalogue "
    "(organization_id, project_id) ON DELETE RESTRICT\n"
    ")"
)
_CREATE_PROJECTION_RECEIPT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.projection_receipt (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    operation_id uuid NOT NULL,\n"
    "    projection_receipt_id varchar(300) NOT NULL "
    "CHECK (projection_receipt_id <> '' "
    "AND projection_receipt_id = btrim(projection_receipt_id)),\n"
    "    data_plane_receipt_hash varchar(71) NOT NULL "
    "CHECK (data_plane_receipt_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    projection_hash varchar(71) NOT NULL "
    "CHECK (projection_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    projection_revision bigint NOT NULL CHECK (projection_revision > 0),\n"
    "    projected_at timestamp with time zone NOT NULL,\n"
    "    PRIMARY KEY (organization_id, operation_id),\n"
    "    UNIQUE (organization_id, projection_receipt_id),\n"
    "    FOREIGN KEY (organization_id, operation_id) "
    "REFERENCES impodo_control.data_plane_receipt "
    "(organization_id, operation_id) ON DELETE RESTRICT\n"
    ")"
)
_CREATE_WORKSPACE_LEASE_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.workspace_lease (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    project_id uuid NOT NULL,\n"
    "    workspace_id uuid NOT NULL,\n"
    "    lease_id uuid NOT NULL,\n"
    "    holder_attempt_id uuid NOT NULL,\n"
    "    fencing_token bigint NOT NULL CHECK (fencing_token > 0),\n"
    "    acquired_at timestamp with time zone NOT NULL,\n"
    "    heartbeat_at timestamp with time zone NOT NULL,\n"
    "    expires_at timestamp with time zone NOT NULL,\n"
    "    version bigint NOT NULL CHECK (version > 0),\n"
    "    released_at timestamp with time zone,\n"
    "    PRIMARY KEY (organization_id, project_id, workspace_id),\n"
    "    UNIQUE (organization_id, lease_id),\n"
    "    FOREIGN KEY (organization_id, project_id) "
    "REFERENCES impodo_control.project_catalogue "
    "(organization_id, project_id) ON DELETE RESTRICT,\n"
    "    CHECK (acquired_at <= heartbeat_at AND heartbeat_at < expires_at),\n"
    "    CHECK (released_at IS NULL OR released_at >= heartbeat_at)\n"
    ")"
)
_CREATE_WORKSPACE_LEASE_EXPIRY_INDEX_QUERY: LiteralString = (
    "CREATE INDEX workspace_lease_organization_expiry_idx\n"
    "ON impodo_control.workspace_lease "
    "(organization_id, project_id, expires_at, workspace_id) "
    "WHERE released_at IS NULL"
)
_GRANT_OPERATION_REQUEST_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.operation_request TO impodo_runtime"
)
_GRANT_DATA_PLANE_RECEIPT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.data_plane_receipt TO impodo_runtime"
)
_GRANT_PROJECTION_RECEIPT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.projection_receipt TO impodo_runtime"
)
_GRANT_WORKSPACE_LEASE_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.workspace_lease TO impodo_runtime"
)
_CREATE_PROJECTION_CURRENT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.projection_current (\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    project_id uuid NOT NULL,\n"
    "    projection_kind varchar(300) NOT NULL "
    "CHECK (projection_kind <> '' "
    "AND projection_kind = btrim(projection_kind)),\n"
    "    operation_id uuid NOT NULL,\n"
    "    data_plane_receipt_hash varchar(71) NOT NULL "
    "CHECK (data_plane_receipt_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    projection_hash varchar(71) NOT NULL "
    "CHECK (projection_hash ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    projection_revision bigint NOT NULL CHECK (projection_revision > 0),\n"
    "    source_committed_at timestamp with time zone NOT NULL,\n"
    "    projected_at timestamp with time zone NOT NULL,\n"
    "    updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp(),\n"
    "    PRIMARY KEY (organization_id, project_id, projection_kind),\n"
    "    UNIQUE (organization_id, operation_id),\n"
    "    FOREIGN KEY (organization_id, project_id) "
    "REFERENCES impodo_control.project_catalogue "
    "(organization_id, project_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (organization_id, operation_id) "
    "REFERENCES impodo_control.projection_receipt "
    "(organization_id, operation_id) ON DELETE RESTRICT\n"
    ")"
)
_BACKFILL_PROJECTION_CURRENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.projection_current (\n"
    "    organization_id, project_id, projection_kind, operation_id,\n"
    "    data_plane_receipt_hash, projection_hash, projection_revision,\n"
    "    source_committed_at, projected_at, updated_at\n"
    ")\n"
    "SELECT DISTINCT ON (o.organization_id, o.project_id, o.operation_kind)\n"
    "       o.organization_id, o.project_id, o.operation_kind, o.operation_id,\n"
    "       p.data_plane_receipt_hash, p.projection_hash,\n"
    "       p.projection_revision, d.committed_at, p.projected_at,\n"
    "       transaction_timestamp()\n"
    "FROM impodo_control.operation_request AS o\n"
    "JOIN impodo_control.data_plane_receipt AS d\n"
    "  ON d.organization_id = o.organization_id\n"
    " AND d.operation_id = o.operation_id\n"
    "JOIN impodo_control.projection_receipt AS p\n"
    "  ON p.organization_id = o.organization_id\n"
    " AND p.operation_id = o.operation_id\n"
    "WHERE o.state = 'PROJECTED'\n"
    "ORDER BY o.organization_id, o.project_id, o.operation_kind,\n"
    "         d.committed_at DESC, o.operation_id DESC"
)
_GRANT_PROJECTION_CURRENT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.projection_current TO impodo_runtime"
)
_CREATE_PLATFORM_ADMINISTRATOR_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.platform_administrator (\n"
    "    administrator_id uuid PRIMARY KEY,\n"
    "    normalized_login_email varchar(320) NOT NULL UNIQUE "
    "CHECK (normalized_login_email <> '' "
    "AND normalized_login_email = btrim(normalized_login_email)),\n"
    "    display_name varchar(200) NOT NULL "
    "CHECK (display_name <> '' AND display_name = btrim(display_name)),\n"
    "    status varchar(32) NOT NULL "
    "CHECK (status IN ('ACTIVE', 'DEACTIVATED')),\n"
    "    security_revision bigint NOT NULL CHECK (security_revision > 0),\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    deactivated_at timestamp with time zone,\n"
    "    CHECK ((status = 'ACTIVE' AND deactivated_at IS NULL) "
    "OR (status = 'DEACTIVATED' AND deactivated_at IS NOT NULL))\n"
    ")"
)
_CREATE_ACTIVE_PLATFORM_ADMINISTRATOR_INDEX_QUERY: LiteralString = (
    "CREATE INDEX platform_administrator_active_created_idx\n"
    "ON impodo_control.platform_administrator (created_at, administrator_id)\n"
    "WHERE status = 'ACTIVE'"
)
_CREATE_PLATFORM_ADMINISTRATOR_PASSWORD_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.platform_administrator_password_identity (\n"
    "    administrator_id uuid PRIMARY KEY,\n"
    "    algorithm varchar(64) NOT NULL "
    "CHECK (algorithm <> '' AND algorithm = btrim(algorithm)),\n"
    "    parameters jsonb NOT NULL "
    "CHECK (jsonb_typeof(parameters) = 'object'),\n"
    "    salt bytea NOT NULL CHECK (octet_length(salt) BETWEEN 16 AND 128),\n"
    "    password_hash bytea NOT NULL "
    "CHECK (octet_length(password_hash) BETWEEN 32 AND 256),\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT\n"
    ")"
)
_CREATE_PLATFORM_SECURITY_EVENT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.platform_security_event (\n"
    "    event_id uuid PRIMARY KEY,\n"
    "    deployment_id varchar(63) NOT NULL "
    "CHECK (deployment_id <> '' AND deployment_id = btrim(deployment_id)),\n"
    "    event_kind varchar(100) NOT NULL "
    "CHECK (event_kind IN ('PLATFORM_ADMINISTRATOR_BOOTSTRAPPED')),\n"
    "    subject_administrator_id uuid NOT NULL,\n"
    "    occurred_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (subject_administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT\n"
    ")"
)
_CREATE_PLATFORM_SECURITY_EVENT_INDEX_QUERY: LiteralString = (
    "CREATE INDEX platform_security_event_subject_time_idx\n"
    "ON impodo_control.platform_security_event "
    "(subject_administrator_id, occurred_at DESC, event_id)"
)
_GRANT_PLATFORM_ADMINISTRATOR_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.platform_administrator TO impodo_runtime"
)
_GRANT_PLATFORM_ADMINISTRATOR_PASSWORD_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.platform_administrator_password_identity TO impodo_runtime"
)
_GRANT_PLATFORM_SECURITY_EVENT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.platform_security_event TO impodo_runtime"
)
_ADD_PLATFORM_PASSWORD_REVISION_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.platform_administrator_password_identity\n"
    "ADD COLUMN password_revision bigint NOT NULL DEFAULT 1 "
    "CHECK (password_revision > 0),\n"
    "ADD COLUMN updated_at timestamp with time zone NOT NULL "
    "DEFAULT transaction_timestamp()"
)
_CREATE_LOGIN_IDENTITY_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.login_identity (\n"
    "    normalized_login_email varchar(320) PRIMARY KEY "
    "CHECK (normalized_login_email <> '' "
    "AND normalized_login_email = btrim(normalized_login_email)),\n"
    "    principal_kind varchar(32) NOT NULL "
    "CHECK (principal_kind = 'PLATFORM_ADMINISTRATOR'),\n"
    "    platform_administrator_id uuid NOT NULL UNIQUE,\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (platform_administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT\n"
    ")"
)
_BACKFILL_LOGIN_IDENTITY_QUERY: LiteralString = (
    "INSERT INTO impodo_control.login_identity (\n"
    "    normalized_login_email, principal_kind, "
    "platform_administrator_id, created_at\n"
    ")\n"
    "SELECT normalized_login_email, 'PLATFORM_ADMINISTRATOR', "
    "administrator_id, created_at\n"
    "FROM impodo_control.platform_administrator\n"
    "ORDER BY created_at, administrator_id"
)
_CREATE_AUTHENTICATION_THROTTLE_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.authentication_throttle (\n"
    "    scope_kind varchar(16) NOT NULL "
    "CHECK (scope_kind IN ('ACCOUNT', 'SOURCE')),\n"
    "    scope_digest varchar(71) NOT NULL "
    "CHECK (scope_digest ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    failure_count integer NOT NULL "
    "CHECK (failure_count BETWEEN 0 AND 1000),\n"
    "    window_started_at timestamp with time zone NOT NULL,\n"
    "    locked_until timestamp with time zone,\n"
    "    optimistic_revision bigint NOT NULL "
    "CHECK (optimistic_revision > 0),\n"
    "    updated_at timestamp with time zone NOT NULL,\n"
    "    PRIMARY KEY (scope_kind, scope_digest)\n"
    ")"
)
_EXPAND_PLATFORM_SECURITY_EVENT_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.platform_security_event\n"
    "DROP CONSTRAINT platform_security_event_event_kind_check,\n"
    "ALTER COLUMN subject_administrator_id DROP NOT NULL,\n"
    "ADD COLUMN principal_hint_digest varchar(71) "
    "CHECK (principal_hint_digest ~ '^sha256:[0-9a-f]{64}$'),\n"
    "ADD COLUMN source_digest varchar(71) "
    "CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),\n"
    "ADD CONSTRAINT platform_security_event_event_kind_check "
    "CHECK (event_kind IN ("
    "'PLATFORM_ADMINISTRATOR_BOOTSTRAPPED', "
    "'AUTHENTICATION_SUCCEEDED', 'AUTHENTICATION_REJECTED', "
    "'PASSWORD_REHASHED'))"
)
_GRANT_LOGIN_IDENTITY_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.login_identity TO impodo_runtime"
)
_GRANT_AUTHENTICATION_THROTTLE_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.authentication_throttle TO impodo_runtime"
)
_GRANT_PLATFORM_PASSWORD_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (algorithm, parameters, salt, password_hash, "
    "password_revision, updated_at) "
    "ON impodo_control.platform_administrator_password_identity "
    "TO impodo_runtime"
)
_ADD_LOGIN_PRINCIPAL_ID_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.login_identity "
    "ADD COLUMN principal_id uuid"
)
_BACKFILL_LOGIN_PRINCIPAL_ID_QUERY: LiteralString = (
    "UPDATE impodo_control.login_identity "
    "SET principal_id = platform_administrator_id"
)
_ENFORCE_LOGIN_PRINCIPAL_ID_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.login_identity "
    "ALTER COLUMN principal_id SET NOT NULL, "
    "ADD CONSTRAINT login_identity_principal_unique "
    "UNIQUE (principal_kind, principal_id)"
)
_EXPAND_AUTHENTICATION_THROTTLE_SCOPES_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.authentication_throttle\n"
    "DROP CONSTRAINT authentication_throttle_scope_kind_check,\n"
    "ADD CONSTRAINT authentication_throttle_scope_kind_check "
    "CHECK (scope_kind IN "
    "('ACCOUNT', 'SOURCE', 'RESET_ACCOUNT', 'RESET_SOURCE'))"
)
_EXPAND_RECOVERY_SECURITY_EVENTS_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.platform_security_event\n"
    "DROP CONSTRAINT platform_security_event_event_kind_check,\n"
    "ADD CONSTRAINT platform_security_event_event_kind_check "
    "CHECK (event_kind IN ("
    "'PLATFORM_ADMINISTRATOR_BOOTSTRAPPED', "
    "'AUTHENTICATION_SUCCEEDED', 'AUTHENTICATION_REJECTED', "
    "'PASSWORD_REHASHED', 'PASSWORD_RESET_REQUESTED', "
    "'RECOVERY_TOKEN_ISSUED', 'PASSWORD_RESET_COMPLETED'))"
)
_CREATE_ACCOUNT_RECOVERY_TOKEN_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.account_recovery_token (\n"
    "    token_id uuid PRIMARY KEY,\n"
    "    principal_kind varchar(32) NOT NULL,\n"
    "    principal_id uuid NOT NULL,\n"
    "    purpose varchar(32) NOT NULL "
    "CHECK (purpose IN ('ACTIVATION', 'PASSWORD_RESET')),\n"
    "    token_digest bytea NOT NULL UNIQUE "
    "CHECK (octet_length(token_digest) = 32),\n"
    "    state varchar(16) NOT NULL "
    "CHECK (state IN ('ISSUED', 'CONSUMED', 'REVOKED')),\n"
    "    issued_security_revision bigint NOT NULL "
    "CHECK (issued_security_revision > 0),\n"
    "    issued_password_revision bigint "
    "CHECK (issued_password_revision IS NULL "
    "OR issued_password_revision > 0),\n"
    "    issued_by_administrator_id uuid,\n"
    "    issued_at timestamp with time zone NOT NULL,\n"
    "    expires_at timestamp with time zone NOT NULL,\n"
    "    consumed_at timestamp with time zone,\n"
    "    revoked_at timestamp with time zone,\n"
    "    optimistic_revision bigint NOT NULL "
    "CHECK (optimistic_revision > 0),\n"
    "    FOREIGN KEY (principal_kind, principal_id) "
    "REFERENCES impodo_control.login_identity "
    "(principal_kind, principal_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (issued_by_administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT,\n"
    "    CHECK (expires_at > issued_at),\n"
    "    CHECK ((purpose = 'PASSWORD_RESET' "
    "AND issued_password_revision IS NOT NULL) "
    "OR purpose = 'ACTIVATION'),\n"
    "    CHECK ((state = 'ISSUED' AND consumed_at IS NULL "
    "AND revoked_at IS NULL) "
    "OR (state = 'CONSUMED' AND consumed_at IS NOT NULL "
    "AND revoked_at IS NULL) "
    "OR (state = 'REVOKED' AND consumed_at IS NULL "
    "AND revoked_at IS NOT NULL))\n"
    ")"
)
_CREATE_ACCOUNT_RECOVERY_OUTSTANDING_INDEX_QUERY: LiteralString = (
    "CREATE UNIQUE INDEX account_recovery_token_outstanding_idx\n"
    "ON impodo_control.account_recovery_token "
    "(principal_kind, principal_id, purpose)\n"
    "WHERE state = 'ISSUED'"
)
_CREATE_ACCOUNT_RECOVERY_EXPIRY_INDEX_QUERY: LiteralString = (
    "CREATE INDEX account_recovery_token_expiry_idx\n"
    "ON impodo_control.account_recovery_token "
    "(expires_at, token_id) WHERE state = 'ISSUED'"
)
_GRANT_ACCOUNT_RECOVERY_TOKEN_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.account_recovery_token "
    "TO impodo_runtime"
)
_GRANT_ACCOUNT_RECOVERY_TOKEN_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (state, consumed_at, revoked_at, optimistic_revision) "
    "ON impodo_control.account_recovery_token TO impodo_runtime"
)
_GRANT_PLATFORM_SECURITY_REVISION_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (security_revision) "
    "ON impodo_control.platform_administrator TO impodo_runtime"
)
_EXPAND_SESSION_SECURITY_EVENTS_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.platform_security_event\n"
    "DROP CONSTRAINT platform_security_event_event_kind_check,\n"
    "ADD COLUMN actor_administrator_id uuid,\n"
    "ADD CONSTRAINT platform_security_event_actor_fk "
    "FOREIGN KEY (actor_administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT,\n"
    "ADD CONSTRAINT platform_security_event_event_kind_check "
    "CHECK (event_kind IN ("
    "'PLATFORM_ADMINISTRATOR_BOOTSTRAPPED', "
    "'AUTHENTICATION_SUCCEEDED', 'AUTHENTICATION_REJECTED', "
    "'PASSWORD_REHASHED', 'PASSWORD_RESET_REQUESTED', "
    "'RECOVERY_TOKEN_ISSUED', 'PASSWORD_RESET_COMPLETED', "
    "'SESSION_CREATED', 'SESSION_ROTATED', 'SESSION_REVOKED'))"
)
_CREATE_SERVER_SESSION_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.server_session (\n"
    "    session_id uuid PRIMARY KEY,\n"
    "    principal_kind varchar(32) NOT NULL,\n"
    "    principal_id uuid NOT NULL,\n"
    "    organization_id varchar(200),\n"
    "    handle_digest bytea NOT NULL UNIQUE "
    "CHECK (octet_length(handle_digest) = 32),\n"
    "    csrf_digest bytea NOT NULL "
    "CHECK (octet_length(csrf_digest) = 32),\n"
    "    authentication_method varchar(32) NOT NULL "
    "CHECK (authentication_method IN ('PASSWORD', 'WEBAUTHN')),\n"
    "    security_revision bigint NOT NULL CHECK (security_revision > 0),\n"
    "    state varchar(16) NOT NULL "
    "CHECK (state IN ('ACTIVE', 'REVOKED')),\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    last_seen_at timestamp with time zone NOT NULL,\n"
    "    idle_expires_at timestamp with time zone NOT NULL,\n"
    "    absolute_expires_at timestamp with time zone NOT NULL,\n"
    "    elevated_until timestamp with time zone,\n"
    "    revoked_at timestamp with time zone,\n"
    "    revocation_reason varchar(32) "
    "CHECK (revocation_reason IN ("
    "'ADMINISTRATOR', 'ACCOUNT_SUSPENDED', 'EXPIRED', "
    "'ORGANIZATION_SUSPENDED', 'PASSWORD_RESET', "
    "'SECURITY_REVISION')),\n"
    "    optimistic_revision bigint NOT NULL "
    "CHECK (optimistic_revision > 0),\n"
    "    FOREIGN KEY (principal_kind, principal_id) "
    "REFERENCES impodo_control.login_identity "
    "(principal_kind, principal_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    CHECK (principal_kind <> 'PLATFORM_ADMINISTRATOR' "
    "OR organization_id IS NULL),\n"
    "    CHECK (created_at <= last_seen_at "
    "AND last_seen_at < idle_expires_at "
    "AND idle_expires_at <= absolute_expires_at),\n"
    "    CHECK (elevated_until IS NULL "
    "OR (last_seen_at < elevated_until "
    "AND elevated_until <= absolute_expires_at)),\n"
    "    CHECK ((state = 'ACTIVE' AND revoked_at IS NULL "
    "AND revocation_reason IS NULL) "
    "OR (state = 'REVOKED' AND revoked_at IS NOT NULL "
    "AND revocation_reason IS NOT NULL))\n"
    ")"
)
_CREATE_ACTIVE_SERVER_SESSION_SUBJECT_INDEX_QUERY: LiteralString = (
    "CREATE INDEX server_session_active_subject_expiry_idx\n"
    "ON impodo_control.server_session "
    "(principal_kind, principal_id, absolute_expires_at, session_id)\n"
    "WHERE state = 'ACTIVE'"
)
_CREATE_ACTIVE_SERVER_SESSION_ORGANIZATION_INDEX_QUERY: LiteralString = (
    "CREATE INDEX server_session_active_organization_expiry_idx\n"
    "ON impodo_control.server_session "
    "(organization_id, absolute_expires_at, session_id)\n"
    "WHERE state = 'ACTIVE' AND organization_id IS NOT NULL"
)
_GRANT_SERVER_SESSION_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.server_session TO impodo_runtime"
)
_GRANT_SERVER_SESSION_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (handle_digest, csrf_digest, authentication_method, state, "
    "last_seen_at, idle_expires_at, elevated_until, revoked_at, "
    "revocation_reason, optimistic_revision) "
    "ON impodo_control.server_session TO impodo_runtime"
)
_CREATE_CUSTOMER_USER_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.customer_user (\n"
    "    user_id uuid PRIMARY KEY,\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    normalized_login_email varchar(320) NOT NULL UNIQUE "
    "CHECK (normalized_login_email <> '' "
    "AND normalized_login_email = btrim(normalized_login_email)),\n"
    "    display_name varchar(200) NOT NULL "
    "CHECK (display_name <> '' AND display_name = btrim(display_name)),\n"
    "    principal_kind varchar(32) NOT NULL "
    "CHECK (principal_kind IN "
    "('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER')),\n"
    "    status varchar(32) NOT NULL "
    "CHECK (status IN "
    "('PENDING_ACTIVATION', 'ACTIVE', 'SUSPENDED', 'DEACTIVATED')),\n"
    "    security_revision bigint NOT NULL CHECK (security_revision > 0),\n"
    "    optimistic_revision bigint NOT NULL CHECK (optimistic_revision > 0),\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    activated_at timestamp with time zone,\n"
    "    suspended_at timestamp with time zone,\n"
    "    deactivated_at timestamp with time zone,\n"
    "    updated_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    CHECK ((status = 'PENDING_ACTIVATION' "
    "AND activated_at IS NULL AND suspended_at IS NULL "
    "AND deactivated_at IS NULL) "
    "OR (status = 'ACTIVE' AND activated_at IS NOT NULL "
    "AND suspended_at IS NULL AND deactivated_at IS NULL) "
    "OR (status = 'SUSPENDED' AND activated_at IS NOT NULL "
    "AND suspended_at IS NOT NULL AND deactivated_at IS NULL) "
    "OR (status = 'DEACTIVATED' AND deactivated_at IS NOT NULL))\n"
    ")"
)
_CREATE_CUSTOMER_USER_ORGANIZATION_INDEX_QUERY: LiteralString = (
    "CREATE INDEX customer_user_organization_status_idx\n"
    "ON impodo_control.customer_user "
    "(organization_id, status, display_name, user_id)"
)
_CREATE_CUSTOMER_PASSWORD_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.customer_user_password_identity (\n"
    "    user_id uuid PRIMARY KEY,\n"
    "    algorithm varchar(64) NOT NULL "
    "CHECK (algorithm <> '' AND algorithm = btrim(algorithm)),\n"
    "    parameters jsonb NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),\n"
    "    salt bytea NOT NULL CHECK (octet_length(salt) BETWEEN 16 AND 128),\n"
    "    password_hash bytea NOT NULL "
    "CHECK (octet_length(password_hash) BETWEEN 32 AND 256),\n"
    "    password_revision bigint NOT NULL CHECK (password_revision > 0),\n"
    "    created_at timestamp with time zone NOT NULL,\n"
    "    updated_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (user_id) "
    "REFERENCES impodo_control.customer_user (user_id) ON DELETE RESTRICT\n"
    ")"
)
_EXPAND_LOGIN_IDENTITY_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.login_identity\n"
    "DROP CONSTRAINT login_identity_principal_kind_check,\n"
    "ALTER COLUMN platform_administrator_id DROP NOT NULL,\n"
    "ADD COLUMN customer_user_id uuid UNIQUE,\n"
    "ADD CONSTRAINT login_identity_customer_user_fk "
    "FOREIGN KEY (customer_user_id) "
    "REFERENCES impodo_control.customer_user (user_id) ON DELETE RESTRICT,\n"
    "ADD CONSTRAINT login_identity_principal_kind_check "
    "CHECK (principal_kind IN "
    "('PLATFORM_ADMINISTRATOR', 'ORGANIZATION_ADMINISTRATOR', "
    "'CUSTOMER_USER')),\n"
    "ADD CONSTRAINT login_identity_exact_subject_check CHECK ("
    "(principal_kind = 'PLATFORM_ADMINISTRATOR' "
    "AND platform_administrator_id IS NOT NULL "
    "AND customer_user_id IS NULL "
    "AND principal_id = platform_administrator_id) OR "
    "(principal_kind IN ('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER') "
    "AND platform_administrator_id IS NULL "
    "AND customer_user_id IS NOT NULL "
    "AND principal_id = customer_user_id))"
)
_CREATE_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.organization_administrator_assignment (\n"
    "    organization_id varchar(200) PRIMARY KEY,\n"
    "    user_id uuid NOT NULL UNIQUE,\n"
    "    assignment_revision bigint NOT NULL "
    "CHECK (assignment_revision > 0),\n"
    "    assigned_by_administrator_id uuid NOT NULL,\n"
    "    assigned_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (user_id) "
    "REFERENCES impodo_control.customer_user (user_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (assigned_by_administrator_id) "
    "REFERENCES impodo_control.platform_administrator (administrator_id) "
    "ON DELETE RESTRICT\n"
    ")"
)
_CREATE_IDENTITY_ADMINISTRATION_EVENT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.identity_administration_event (\n"
    "    event_id uuid PRIMARY KEY,\n"
    "    deployment_id varchar(63) NOT NULL "
    "CHECK (deployment_id <> '' AND deployment_id = btrim(deployment_id)),\n"
    "    event_kind varchar(100) NOT NULL "
    "CHECK (event_kind IN ("
    "'ORGANIZATION_CREATED', 'ORGANIZATION_RENAMED', "
    "'ORGANIZATION_SUSPENDED', 'ORGANIZATION_REACTIVATED', "
    "'ORGANIZATION_POLICY_REVISED', "
    "'ORGANIZATION_ADMINISTRATOR_ASSIGNED', "
    "'ORGANIZATION_ADMINISTRATOR_REPLACED', 'CUSTOMER_USER_CREATED', "
    "'CUSTOMER_USER_ACTIVATED', 'CUSTOMER_USER_SUSPENDED', "
    "'CUSTOMER_USER_REACTIVATED', 'CUSTOMER_USER_DEACTIVATED', "
    "'CUSTOMER_RESET_TOKEN_ISSUED', 'CUSTOMER_PASSWORD_RESET', "
    "'CUSTOMER_SESSION_REVOKED')),\n"
    "    organization_id varchar(200) NOT NULL,\n"
    "    subject_user_id uuid,\n"
    "    actor_principal_kind varchar(32),\n"
    "    actor_principal_id uuid,\n"
    "    occurred_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (subject_user_id) "
    "REFERENCES impodo_control.customer_user (user_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (actor_principal_kind, actor_principal_id) "
    "REFERENCES impodo_control.login_identity "
    "(principal_kind, principal_id) ON DELETE RESTRICT,\n"
    "    CHECK ((actor_principal_kind IS NULL) "
    "= (actor_principal_id IS NULL))\n"
    ")"
)
_CREATE_IDENTITY_ADMINISTRATION_EVENT_INDEX_QUERY: LiteralString = (
    "CREATE INDEX identity_administration_event_organization_time_idx\n"
    "ON impodo_control.identity_administration_event "
    "(organization_id, occurred_at DESC, event_id DESC)"
)
_EXPAND_RECOVERY_TOKEN_ISSUER_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.account_recovery_token\n"
    "ADD COLUMN issued_by_principal_kind varchar(32),\n"
    "ADD COLUMN issued_by_principal_id uuid,\n"
    "ADD CONSTRAINT account_recovery_token_issuer_fk "
    "FOREIGN KEY (issued_by_principal_kind, issued_by_principal_id) "
    "REFERENCES impodo_control.login_identity "
    "(principal_kind, principal_id) ON DELETE RESTRICT,\n"
    "ADD CONSTRAINT account_recovery_token_issuer_pair_check CHECK ("
    "(issued_by_principal_kind IS NULL) = (issued_by_principal_id IS NULL))"
)
_BACKFILL_RECOVERY_TOKEN_ISSUER_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET issued_by_principal_kind = 'PLATFORM_ADMINISTRATOR',\n"
    "    issued_by_principal_id = issued_by_administrator_id\n"
    "WHERE issued_by_administrator_id IS NOT NULL"
)
_REVOKE_ORGANIZATION_UPDATE_QUERY: LiteralString = (
    "REVOKE UPDATE ON impodo_control.organization FROM impodo_runtime"
)
_GRANT_ORGANIZATION_LIFECYCLE_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (display_name, status, policy_revision, "
    "optimistic_revision, updated_at, suspended_at) "
    "ON impodo_control.organization TO impodo_runtime"
)
_GRANT_CUSTOMER_USER_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.customer_user TO impodo_runtime"
)
_GRANT_CUSTOMER_USER_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (status, security_revision, optimistic_revision, "
    "activated_at, suspended_at, deactivated_at, updated_at) "
    "ON impodo_control.customer_user TO impodo_runtime"
)
_GRANT_CUSTOMER_PASSWORD_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.customer_user_password_identity "
    "TO impodo_runtime"
)
_GRANT_CUSTOMER_PASSWORD_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (algorithm, parameters, salt, password_hash, "
    "password_revision, updated_at) "
    "ON impodo_control.customer_user_password_identity TO impodo_runtime"
)
_GRANT_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT, UPDATE "
    "ON impodo_control.organization_administrator_assignment TO impodo_runtime"
)
_GRANT_IDENTITY_ADMINISTRATION_EVENT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT "
    "ON impodo_control.identity_administration_event TO impodo_runtime"
)
_BIND_CUSTOMER_USER_TENANT_KIND_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.customer_user\n"
    "ADD CONSTRAINT customer_user_organization_user_kind_key "
    "UNIQUE (organization_id, user_id, principal_kind),\n"
    "ADD CONSTRAINT customer_user_user_kind_key UNIQUE (user_id, principal_kind)"
)
_BIND_LOGIN_IDENTITY_CUSTOMER_KIND_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.login_identity\n"
    "ADD CONSTRAINT login_identity_customer_kind_fk "
    "FOREIGN KEY (customer_user_id, principal_kind) "
    "REFERENCES impodo_control.customer_user (user_id, principal_kind) "
    "ON DELETE RESTRICT"
)
_BIND_ORGANIZATION_ADMINISTRATOR_TENANT_KIND_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.organization_administrator_assignment\n"
    "ADD COLUMN principal_kind varchar(32) NOT NULL "
    "DEFAULT 'ORGANIZATION_ADMINISTRATOR',\n"
    "ADD CONSTRAINT organization_administrator_assignment_kind_check "
    "CHECK (principal_kind = 'ORGANIZATION_ADMINISTRATOR'),\n"
    "ADD CONSTRAINT organization_administrator_assignment_tenant_kind_fk "
    "FOREIGN KEY (organization_id, user_id, principal_kind) "
    "REFERENCES impodo_control.customer_user "
    "(organization_id, user_id, principal_kind) ON DELETE RESTRICT"
)
_DROP_ORGANIZATION_ADMINISTRATOR_KIND_DEFAULT_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.organization_administrator_assignment "
    "ALTER COLUMN principal_kind DROP DEFAULT"
)
_REVOKE_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_UPDATE_QUERY: LiteralString = (
    "REVOKE UPDATE ON impodo_control.organization_administrator_assignment "
    "FROM impodo_runtime"
)
_GRANT_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_UPDATE_QUERY: LiteralString = (
    "GRANT UPDATE (user_id, assignment_revision, "
    "assigned_by_administrator_id, assigned_at) "
    "ON impodo_control.organization_administrator_assignment TO impodo_runtime"
)
_BIND_CUSTOMER_SESSION_TENANT_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.server_session\n"
    "ADD CONSTRAINT server_session_exact_subject_check CHECK ("
    "(principal_kind = 'PLATFORM_ADMINISTRATOR' "
    "AND organization_id IS NULL) OR "
    "(principal_kind IN ('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER') "
    "AND organization_id IS NOT NULL)),\n"
    "ADD CONSTRAINT server_session_customer_tenant_kind_fk "
    "FOREIGN KEY (organization_id, principal_id, principal_kind) "
    "REFERENCES impodo_control.customer_user "
    "(organization_id, user_id, principal_kind) ON DELETE RESTRICT"
)
_ADD_CUSTOMER_USER_TENANT_IDENTITY_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.customer_user\n"
    "ADD CONSTRAINT customer_user_organization_user_key "
    "UNIQUE (organization_id, user_id)"
)
_BIND_ADMINISTRATION_EVENT_TENANT_QUERY: LiteralString = (
    "ALTER TABLE impodo_control.identity_administration_event\n"
    "ADD CONSTRAINT identity_administration_event_subject_tenant_fk "
    "FOREIGN KEY (organization_id, subject_user_id) "
    "REFERENCES impodo_control.customer_user (organization_id, user_id) "
    "ON DELETE RESTRICT"
)
_CREATE_CUSTOMER_USER_DISPLAY_INDEX_QUERY: LiteralString = (
    "CREATE INDEX customer_user_organization_display_idx\n"
    "ON impodo_control.customer_user "
    "(organization_id, display_name, user_id)"
)
_CREATE_SERVER_SECURITY_EVENT_QUERY: LiteralString = (
    "CREATE TABLE impodo_control.server_security_event (\n"
    "    event_id uuid PRIMARY KEY,\n"
    "    deployment_id varchar(63) NOT NULL "
    "CHECK (deployment_id <> '' AND deployment_id = btrim(deployment_id)),\n"
    "    event_kind varchar(64) NOT NULL "
    "CHECK (event_kind IN ("
    "'AUTHENTICATION_SUCCEEDED', 'AUTHENTICATION_REJECTED', "
    "'PASSWORD_REHASHED', 'SESSION_CREATED', 'SESSION_ROTATED', "
    "'SESSION_REVOKED')),\n"
    "    principal_kind varchar(32),\n"
    "    principal_id uuid,\n"
    "    organization_id varchar(200),\n"
    "    principal_hint_digest varchar(71) "
    "CHECK (principal_hint_digest IS NULL OR "
    "principal_hint_digest ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    source_digest varchar(71) "
    "CHECK (source_digest IS NULL OR source_digest ~ '^sha256:[0-9a-f]{64}$'),\n"
    "    occurred_at timestamp with time zone NOT NULL,\n"
    "    FOREIGN KEY (principal_kind, principal_id) "
    "REFERENCES impodo_control.login_identity "
    "(principal_kind, principal_id) ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (organization_id) "
    "REFERENCES impodo_control.organization (organization_id) "
    "ON DELETE RESTRICT,\n"
    "    FOREIGN KEY (organization_id, principal_id, principal_kind) "
    "REFERENCES impodo_control.customer_user "
    "(organization_id, user_id, principal_kind) ON DELETE RESTRICT,\n"
    "    CHECK ((principal_kind IS NULL) = (principal_id IS NULL)),\n"
    "    CHECK ((principal_kind = 'PLATFORM_ADMINISTRATOR' "
    "AND organization_id IS NULL) OR "
    "(principal_kind IN ('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER') "
    "AND organization_id IS NOT NULL) OR "
    "(principal_kind IS NULL AND organization_id IS NULL)),\n"
    "    CHECK ((event_kind IN ('AUTHENTICATION_SUCCEEDED', "
    "'AUTHENTICATION_REJECTED', 'PASSWORD_REHASHED') "
    "AND principal_hint_digest IS NOT NULL AND source_digest IS NOT NULL) OR "
    "(event_kind IN ('SESSION_CREATED', 'SESSION_ROTATED', "
    "'SESSION_REVOKED') AND principal_kind IS NOT NULL "
    "AND principal_hint_digest IS NULL AND source_digest IS NULL))\n"
    ")"
)
_CREATE_SERVER_SECURITY_EVENT_ORGANIZATION_INDEX_QUERY: LiteralString = (
    "CREATE INDEX server_security_event_organization_time_idx\n"
    "ON impodo_control.server_security_event "
    "(organization_id, occurred_at DESC, event_id DESC) "
    "WHERE organization_id IS NOT NULL"
)
_GRANT_SERVER_SECURITY_EVENT_QUERY: LiteralString = (
    "GRANT SELECT, INSERT ON impodo_control.server_security_event "
    "TO impodo_runtime"
)
_UPDATE_CONTRACT_QUERY: LiteralString = (
    "UPDATE impodo_control.schema_contract\n"
    "SET version = %s, updated_at = transaction_timestamp()\n"
    "WHERE contract_name = %s AND generation = %s AND version = %s"
)

MigrationParameter = str | int


class PostgreSQLMigrationUnavailableError(RuntimeError):
    """Report unavailable migration transport without connection detail."""


class PostgreSQLMigrationExecutionError(RuntimeError):
    """Report a failed migration statement without database detail."""


class PostgreSQLMigrationIncompatibleError(RuntimeError):
    """Report an unknown, changed, or malformed migration history."""


@dataclass(frozen=True, slots=True)
class PostgreSQLMigrationStatement:
    """Hold one static migration statement and its non-secret parameters."""

    query: LiteralString
    parameters: tuple[MigrationParameter, ...] = ()


@dataclass(frozen=True, slots=True)
class PostgreSQLMigrationDefinition:
    """Define one immutable forward migration."""

    version: int
    name: str
    checksum: str
    statements: tuple[PostgreSQLMigrationStatement, ...]


@dataclass(frozen=True, slots=True)
class PostgreSQLMigrationResult:
    """Describe safe migration completion facts for an operator."""

    applied_versions: tuple[int, ...]
    current_version: int


class PostgreSQLMigrationSession(Protocol):
    """Own one bounded PostgreSQL migration transaction."""

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None: ...

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLMigrationSessionFactory(Protocol):
    """Open one transaction-owning PostgreSQL migration session."""

    def __call__(self, dsn: str) -> PostgreSQLMigrationSession: ...


def calculate_postgresql_migration_checksum(
    statements: tuple[PostgreSQLMigrationStatement, ...],
) -> str:
    """Return the stable checksum for one immutable statement sequence."""

    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.query.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(
            json.dumps(
                statement.parameters,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\xff")
    return digest.hexdigest()


# Historical contract values stay literal so a later current-version bump cannot
# silently change an already-applied migration checksum.
_MIGRATION_0001_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_CONTRACT_QUERY),
    PostgreSQLMigrationStatement(
        _INSERT_CONTRACT_QUERY,
        (
            "control",
            "impodo-control-v1",
            1,
        ),
    ),
    PostgreSQLMigrationStatement(_GRANT_SCHEMA_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CONTRACT_QUERY),
)
_MIGRATION_0002_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_ORGANIZATION_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PROJECT_CATALOGUE_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PROJECT_LIST_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PROJECT_REPAIR_INDEX_QUERY),
    PostgreSQLMigrationStatement(_GRANT_ORGANIZATION_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PROJECT_CATALOGUE_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            2,
            "control",
            "impodo-control-v1",
            1,
        ),
    ),
)
_MIGRATION_0003_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_OPERATION_REQUEST_QUERY),
    PostgreSQLMigrationStatement(_CREATE_OPERATION_PROJECT_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_DATA_PLANE_RECEIPT_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PROJECTION_RECEIPT_QUERY),
    PostgreSQLMigrationStatement(_CREATE_WORKSPACE_LEASE_QUERY),
    PostgreSQLMigrationStatement(_CREATE_WORKSPACE_LEASE_EXPIRY_INDEX_QUERY),
    PostgreSQLMigrationStatement(_GRANT_OPERATION_REQUEST_QUERY),
    PostgreSQLMigrationStatement(_GRANT_DATA_PLANE_RECEIPT_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PROJECTION_RECEIPT_QUERY),
    PostgreSQLMigrationStatement(_GRANT_WORKSPACE_LEASE_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            3,
            "control",
            "impodo-control-v1",
            2,
        ),
    ),
)
_MIGRATION_0004_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_PROJECTION_CURRENT_QUERY),
    PostgreSQLMigrationStatement(_BACKFILL_PROJECTION_CURRENT_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PROJECTION_CURRENT_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            4,
            "control",
            "impodo-control-v1",
            3,
        ),
    ),
)
_MIGRATION_0005_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_PLATFORM_ADMINISTRATOR_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ACTIVE_PLATFORM_ADMINISTRATOR_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PLATFORM_ADMINISTRATOR_PASSWORD_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PLATFORM_SECURITY_EVENT_QUERY),
    PostgreSQLMigrationStatement(_CREATE_PLATFORM_SECURITY_EVENT_INDEX_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PLATFORM_ADMINISTRATOR_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PLATFORM_ADMINISTRATOR_PASSWORD_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PLATFORM_SECURITY_EVENT_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            5,
            "control",
            "impodo-control-v1",
            4,
        ),
    ),
)
_MIGRATION_0006_STATEMENTS = (
    PostgreSQLMigrationStatement(_ADD_PLATFORM_PASSWORD_REVISION_QUERY),
    PostgreSQLMigrationStatement(_CREATE_LOGIN_IDENTITY_QUERY),
    PostgreSQLMigrationStatement(_BACKFILL_LOGIN_IDENTITY_QUERY),
    PostgreSQLMigrationStatement(_CREATE_AUTHENTICATION_THROTTLE_QUERY),
    PostgreSQLMigrationStatement(_EXPAND_PLATFORM_SECURITY_EVENT_QUERY),
    PostgreSQLMigrationStatement(_GRANT_LOGIN_IDENTITY_QUERY),
    PostgreSQLMigrationStatement(_GRANT_AUTHENTICATION_THROTTLE_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PLATFORM_PASSWORD_UPDATE_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            6,
            "control",
            "impodo-control-v1",
            5,
        ),
    ),
)
_MIGRATION_0007_STATEMENTS = (
    PostgreSQLMigrationStatement(_ADD_LOGIN_PRINCIPAL_ID_QUERY),
    PostgreSQLMigrationStatement(_BACKFILL_LOGIN_PRINCIPAL_ID_QUERY),
    PostgreSQLMigrationStatement(_ENFORCE_LOGIN_PRINCIPAL_ID_QUERY),
    PostgreSQLMigrationStatement(_EXPAND_AUTHENTICATION_THROTTLE_SCOPES_QUERY),
    PostgreSQLMigrationStatement(_EXPAND_RECOVERY_SECURITY_EVENTS_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ACCOUNT_RECOVERY_TOKEN_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ACCOUNT_RECOVERY_OUTSTANDING_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ACCOUNT_RECOVERY_EXPIRY_INDEX_QUERY),
    PostgreSQLMigrationStatement(_GRANT_ACCOUNT_RECOVERY_TOKEN_QUERY),
    PostgreSQLMigrationStatement(_GRANT_ACCOUNT_RECOVERY_TOKEN_UPDATE_QUERY),
    PostgreSQLMigrationStatement(_GRANT_PLATFORM_SECURITY_REVISION_UPDATE_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            7,
            "control",
            "impodo-control-v1",
            6,
        ),
    ),
)
_MIGRATION_0008_STATEMENTS = (
    PostgreSQLMigrationStatement(_EXPAND_SESSION_SECURITY_EVENTS_QUERY),
    PostgreSQLMigrationStatement(_CREATE_SERVER_SESSION_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ACTIVE_SERVER_SESSION_SUBJECT_INDEX_QUERY),
    PostgreSQLMigrationStatement(
        _CREATE_ACTIVE_SERVER_SESSION_ORGANIZATION_INDEX_QUERY
    ),
    PostgreSQLMigrationStatement(_GRANT_SERVER_SESSION_QUERY),
    PostgreSQLMigrationStatement(_GRANT_SERVER_SESSION_UPDATE_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (
            8,
            "control",
            "impodo-control-v1",
            7,
        ),
    ),
)
_MIGRATION_0009_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_CUSTOMER_USER_QUERY),
    PostgreSQLMigrationStatement(_CREATE_CUSTOMER_USER_ORGANIZATION_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_CUSTOMER_PASSWORD_QUERY),
    PostgreSQLMigrationStatement(_EXPAND_LOGIN_IDENTITY_QUERY),
    PostgreSQLMigrationStatement(_CREATE_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_QUERY),
    PostgreSQLMigrationStatement(_CREATE_IDENTITY_ADMINISTRATION_EVENT_QUERY),
    PostgreSQLMigrationStatement(
        _CREATE_IDENTITY_ADMINISTRATION_EVENT_INDEX_QUERY
    ),
    PostgreSQLMigrationStatement(_EXPAND_RECOVERY_TOKEN_ISSUER_QUERY),
    PostgreSQLMigrationStatement(_BACKFILL_RECOVERY_TOKEN_ISSUER_QUERY),
    PostgreSQLMigrationStatement(_REVOKE_ORGANIZATION_UPDATE_QUERY),
    PostgreSQLMigrationStatement(_GRANT_ORGANIZATION_LIFECYCLE_UPDATE_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CUSTOMER_USER_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CUSTOMER_USER_UPDATE_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CUSTOMER_PASSWORD_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CUSTOMER_PASSWORD_UPDATE_QUERY),
    PostgreSQLMigrationStatement(
        _GRANT_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_QUERY
    ),
    PostgreSQLMigrationStatement(_GRANT_IDENTITY_ADMINISTRATION_EVENT_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (9, "control", "impodo-control-v1", 8),
    ),
)
_MIGRATION_0010_STATEMENTS = (
    PostgreSQLMigrationStatement(_BIND_CUSTOMER_USER_TENANT_KIND_QUERY),
    PostgreSQLMigrationStatement(_BIND_LOGIN_IDENTITY_CUSTOMER_KIND_QUERY),
    PostgreSQLMigrationStatement(
        _BIND_ORGANIZATION_ADMINISTRATOR_TENANT_KIND_QUERY
    ),
    PostgreSQLMigrationStatement(_DROP_ORGANIZATION_ADMINISTRATOR_KIND_DEFAULT_QUERY),
    PostgreSQLMigrationStatement(
        _REVOKE_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_UPDATE_QUERY
    ),
    PostgreSQLMigrationStatement(
        _GRANT_ORGANIZATION_ADMINISTRATOR_ASSIGNMENT_UPDATE_QUERY
    ),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (10, "control", "impodo-control-v1", 9),
    ),
)
_MIGRATION_0011_STATEMENTS = (
    PostgreSQLMigrationStatement(_ADD_CUSTOMER_USER_TENANT_IDENTITY_QUERY),
    PostgreSQLMigrationStatement(_BIND_CUSTOMER_SESSION_TENANT_QUERY),
    PostgreSQLMigrationStatement(_BIND_ADMINISTRATION_EVENT_TENANT_QUERY),
    PostgreSQLMigrationStatement(_CREATE_CUSTOMER_USER_DISPLAY_INDEX_QUERY),
    PostgreSQLMigrationStatement(_CREATE_SERVER_SECURITY_EVENT_QUERY),
    PostgreSQLMigrationStatement(
        _CREATE_SERVER_SECURITY_EVENT_ORGANIZATION_INDEX_QUERY
    ),
    PostgreSQLMigrationStatement(_GRANT_SERVER_SECURITY_EVENT_QUERY),
    PostgreSQLMigrationStatement(
        _UPDATE_CONTRACT_QUERY,
        (11, "control", "impodo-control-v1", 10),
    ),
)

POSTGRESQL_MIGRATIONS = (
    PostgreSQLMigrationDefinition(
        version=1,
        name="create-control-schema-contract",
        checksum="24c4c383ecaaa0626766b8eae24cc17009e69179044a038d556a3ac9a445b556",
        statements=_MIGRATION_0001_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=2,
        name="create-organization-and-project-catalogue",
        checksum="8743d8f6094562522a3573d7186ae7b60ec488ceb884e8b4a2775431d36fe2d1",
        statements=_MIGRATION_0002_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=3,
        name="create-operation-receipt-and-lease-coordination",
        checksum="e7a866832e0a50d4cac2e9b980de0cbb4f2aabe3972c638c880d8b6d233a9175",
        statements=_MIGRATION_0003_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=4,
        name="create-current-projection-pointers",
        checksum="7ced134369ebc673b1f463f571d09fe745e80419a9a521dd1304699a62075dea",
        statements=_MIGRATION_0004_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=5,
        name="create-platform-administrator-bootstrap",
        checksum="9c4514309889deaf90b3b2397ed7a80a34f5486cfd07cd1db75feb0c9667ab0f",
        statements=_MIGRATION_0005_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=6,
        name="add-platform-authentication-policy",
        checksum="b2ae2625460aa2387ccc118925fe7d1114f75c2d78b8966d4c98119de4246cf7",
        statements=_MIGRATION_0006_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=7,
        name="add-one-time-account-recovery-tokens",
        checksum="456196b8188d4013cb43c86843dea4eccfb4fd4538eeb9127ce60625141eb9f9",
        statements=_MIGRATION_0007_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=8,
        name="add-opaque-server-sessions",
        checksum="344fe71b6f37f800cf7f8dd50e8f0e06fd161141a1550ff77e0fce1dede4eca8",
        statements=_MIGRATION_0008_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=9,
        name="add-organization-and-customer-administration",
        checksum="9162590a5ef0bc2bb3b36322f077b5b9cb88ce4b2a6a9562bacae2592c058e02",
        statements=_MIGRATION_0009_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=10,
        name="bind-customer-administrator-tenant-and-kind",
        checksum="5382f483a0fae5183f3ee5fc234208721762452cee53c002a93adfff7c2f78cc",
        statements=_MIGRATION_0010_STATEMENTS,
    ),
    PostgreSQLMigrationDefinition(
        version=11,
        name="bind-customer-sessions-and-server-security-events",
        checksum="5a070c836776133546a1861d67132f85cbd11711b3f7c350aefb56f5468a0fcf",
        statements=_MIGRATION_0011_STATEMENTS,
    ),
)


class _PsycopgMigrationSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def execute(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> None:
        try:
            self._connection.execute(query, parameters)
        except (psycopg.OperationalError, psycopg.InterfaceError) as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration transport failed"
            ) from error
        except psycopg.Error as error:
            raise PostgreSQLMigrationExecutionError(
                "PostgreSQL migration statement failed"
            ) from error

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        try:
            rows = self._connection.execute(query, parameters).fetchall()
        except (psycopg.OperationalError, psycopg.InterfaceError) as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration transport failed"
            ) from error
        except psycopg.Error as error:
            raise PostgreSQLMigrationExecutionError(
                "PostgreSQL migration statement failed"
            ) from error
        return tuple(tuple(row) for row in rows)

    def commit(self) -> None:
        try:
            self._connection.commit()
        except (psycopg.OperationalError, psycopg.InterfaceError) as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration commit failed"
            ) from error
        except psycopg.Error as error:
            raise PostgreSQLMigrationExecutionError(
                "PostgreSQL migration commit failed"
            ) from error

    def rollback(self) -> None:
        try:
            self._connection.rollback()
        except psycopg.Error as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration rollback failed"
            ) from error

    def close(self) -> None:
        try:
            self._connection.close()
        except psycopg.Error as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration connection close failed"
            ) from error


def open_psycopg_migration_session(dsn: str) -> PostgreSQLMigrationSession:
    """Open one bounded read-write transaction as the migration role."""

    options = (
        f"-c role={POSTGRESQL_MIGRATION_ROLE} "
        f"-c statement_timeout={POSTGRESQL_MIGRATION_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_MIGRATION_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=60000"
    )
    try:
        connection = psycopg.connect(
            conninfo=dsn,
            autocommit=False,
            connect_timeout=POSTGRESQL_MIGRATION_CONNECT_TIMEOUT_SECONDS,
            application_name="impodo-migrate",
            options=options,
        )
    except psycopg.Error as error:
        raise PostgreSQLMigrationUnavailableError(
            "PostgreSQL migration connection failed"
        ) from error
    return _PsycopgMigrationSession(connection)


def _validated_applied_count(
    rows: tuple[tuple[object, ...], ...],
    migrations: tuple[PostgreSQLMigrationDefinition, ...],
) -> int:
    if len(rows) > len(migrations):
        raise PostgreSQLMigrationIncompatibleError(
            "PostgreSQL migration history is incompatible"
        )
    for index, row in enumerate(rows):
        migration = migrations[index]
        if row != (migration.version, migration.name, migration.checksum):
            raise PostgreSQLMigrationIncompatibleError(
                "PostgreSQL migration history is incompatible"
            )
    return len(rows)


def _validate_definitions(
    migrations: tuple[PostgreSQLMigrationDefinition, ...],
) -> None:
    if not migrations:
        raise PostgreSQLMigrationExecutionError(
            "PostgreSQL migration definition is invalid"
        )
    for expected_version, migration in enumerate(migrations, start=1):
        checksum = calculate_postgresql_migration_checksum(migration.statements)
        if (
            migration.version != expected_version
            or not migration.name
            or migration.checksum != checksum
        ):
            raise PostgreSQLMigrationExecutionError(
                "PostgreSQL migration definition is invalid"
            )


def _rollback_without_replacing_error(session: PostgreSQLMigrationSession) -> None:
    try:
        session.rollback()
    except (PostgreSQLMigrationUnavailableError, PostgreSQLMigrationExecutionError):
        pass


class PsycopgPostgreSQLMigrator:
    """Apply known migrations once under one transaction-level lock."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        *,
        session_factory: PostgreSQLMigrationSessionFactory = (
            open_psycopg_migration_session
        ),
        migrations: tuple[PostgreSQLMigrationDefinition, ...] = POSTGRESQL_MIGRATIONS,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._session_factory = session_factory
        self._migrations = migrations

    def migrate(self, dsn_reference: PurePosixPath) -> PostgreSQLMigrationResult:
        """Apply the exact known migration suffix and commit it atomically."""

        _validate_definitions(self._migrations)
        try:
            dsn = self._dsn_reader(dsn_reference)
            session = self._session_factory(dsn)
        except PostgreSQLDsnError as error:
            raise PostgreSQLMigrationUnavailableError(
                "PostgreSQL migration connection failed"
            ) from error

        try:
            try:
                result = self._migrate_transaction(session)
                session.commit()
            except PostgreSQLMigrationUnavailableError as error:
                _rollback_without_replacing_error(session)
                raise PostgreSQLMigrationUnavailableError(
                    "PostgreSQL migration service is unavailable"
                ) from error
            except PostgreSQLMigrationExecutionError as error:
                _rollback_without_replacing_error(session)
                raise PostgreSQLMigrationExecutionError(
                    "PostgreSQL migration execution failed"
                ) from error
            except PostgreSQLMigrationIncompatibleError as error:
                _rollback_without_replacing_error(session)
                raise PostgreSQLMigrationIncompatibleError(
                    "PostgreSQL migration history is incompatible"
                ) from error
            return result
        finally:
            active_error = sys.exception()
            try:
                session.close()
            except PostgreSQLMigrationUnavailableError as error:
                if active_error is None:
                    raise PostgreSQLMigrationUnavailableError(
                        "PostgreSQL migration service is unavailable"
                    ) from error

    def _migrate_transaction(
        self,
        session: PostgreSQLMigrationSession,
    ) -> PostgreSQLMigrationResult:
        session.execute(
            _LOCK_QUERY,
            (_MIGRATION_LOCK_NAMESPACE, _MIGRATION_LOCK_RESOURCE),
        )
        session.execute(_CREATE_SCHEMA_QUERY)
        session.execute(_CREATE_LEDGER_QUERY)
        applied_count = _validated_applied_count(
            session.fetch_rows(_READ_LEDGER_QUERY),
            self._migrations,
        )

        applied_versions: list[int] = []
        for migration in self._migrations[applied_count:]:
            for statement in migration.statements:
                session.execute(statement.query, statement.parameters)
            session.execute(
                _INSERT_LEDGER_QUERY,
                (migration.version, migration.name, migration.checksum),
            )
            applied_versions.append(migration.version)

        expected_contract = (
            POSTGRESQL_CONTROL_SCHEMA_GENERATION,
            POSTGRESQL_CONTROL_SCHEMA_VERSION,
        )
        contract_rows = session.fetch_rows(_READ_CONTRACT_QUERY, ("control",))
        if contract_rows != (expected_contract,):
            raise PostgreSQLMigrationIncompatibleError(
                "PostgreSQL schema contract is incompatible"
            )
        return PostgreSQLMigrationResult(
            applied_versions=tuple(applied_versions),
            current_version=self._migrations[-1].version,
        )


__all__ = [
    "POSTGRESQL_MIGRATIONS",
    "POSTGRESQL_MIGRATION_CONNECT_TIMEOUT_SECONDS",
    "POSTGRESQL_MIGRATION_LOCK_TIMEOUT_MILLISECONDS",
    "POSTGRESQL_MIGRATION_ROLE",
    "POSTGRESQL_MIGRATION_STATEMENT_TIMEOUT_MILLISECONDS",
    "POSTGRESQL_RUNTIME_ROLE",
    "PostgreSQLMigrationDefinition",
    "PostgreSQLMigrationExecutionError",
    "PostgreSQLMigrationIncompatibleError",
    "PostgreSQLMigrationResult",
    "PostgreSQLMigrationSession",
    "PostgreSQLMigrationSessionFactory",
    "PostgreSQLMigrationStatement",
    "PostgreSQLMigrationUnavailableError",
    "PsycopgPostgreSQLMigrator",
    "calculate_postgresql_migration_checksum",
    "open_psycopg_migration_session",
]
