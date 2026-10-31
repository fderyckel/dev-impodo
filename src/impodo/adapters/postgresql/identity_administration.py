"""Persist tenant-scoped Organization and customer-account administration."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any, LiteralString, Protocol, TypeVar

import psycopg

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.startup import PostgreSQLDsnError, PostgreSQLDsnReader
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionMetricObserver,
    PostgreSQLTransactionOutcome,
    report_postgresql_transaction_metric,
)
from impodo.domain.identity.administration import (
    AdministrationActor,
    AdministrationEventKind,
    AdministrationEventPage,
    AdministrationEventQuery,
    AdministrationEventView,
    AdministrationSecurityEvent,
    CustomerCredentialMutation,
    CustomerUserDetailQuery,
    CustomerUserListQuery,
    CustomerUserMutationCommand,
    CustomerUserPage,
    CustomerUserRecord,
    CustomerUserStatus,
    CustomerUserView,
    IdentityAdministrationConflictError,
    IdentityAdministrationError,
    IdentityAdministrationNotFoundError,
    IdentityAdministrationStorageError,
    IdentityAdministrationUnavailableError,
    OrganizationAdministrationView,
    OrganizationDetailQuery,
    OrganizationListQuery,
    OrganizationMutationCommand,
    OrganizationPage,
    OrganizationSessionPage,
    OrganizationSessionQuery,
    OrganizationSessionView,
    OrganizationStatus,
    OrganizationView,
    RenameOrganizationCommand,
    ReplaceOrganizationAdministratorCommand,
    ReviseOrganizationPolicyCommand,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.bootstrap import PlatformAdministratorStatus
from impodo.domain.identity.recovery import (
    LoginPrincipalKind,
    RecoveryTokenIssue,
    RecoveryTokenPurpose,
)

POSTGRESQL_IDENTITY_ADMIN_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_IDENTITY_ADMIN_LOCK_TIMEOUT_MILLISECONDS = 5_000
POSTGRESQL_IDENTITY_ADMIN_STATEMENT_TIMEOUT_MILLISECONDS = 10_000
POSTGRESQL_IDENTITY_ADMIN_CONNECTION_BUDGET = 1

_LOCK_PLATFORM_ACTOR_QUERY: LiteralString = (
    "SELECT status FROM impodo_control.platform_administrator\n"
    "WHERE administrator_id = %s\nFOR UPDATE"
)
_LOCK_ORGANIZATION_ACTOR_QUERY: LiteralString = (
    "SELECT u.status, o.status\n"
    "FROM impodo_control.customer_user AS u\n"
    "JOIN impodo_control.organization_administrator_assignment AS a\n"
    "  ON a.organization_id = u.organization_id AND a.user_id = u.user_id\n"
    "JOIN impodo_control.organization AS o\n"
    "  ON o.organization_id = u.organization_id\n"
    "WHERE u.user_id = %s AND u.organization_id = %s\n"
    "  AND u.principal_kind = 'ORGANIZATION_ADMINISTRATOR'\n"
    "FOR UPDATE OF u, a, o"
)
_INSERT_ORGANIZATION_QUERY: LiteralString = (
    "INSERT INTO impodo_control.organization (\n"
    "    organization_id, display_name, status, policy_revision,\n"
    "    optimistic_revision, deletion_state, created_at, updated_at\n"
    ") VALUES (%s, %s, 'ACTIVE', 1, 1, 'NONE', %s, %s)\n"
    "RETURNING organization_id, display_name, status, "
    "policy_revision, optimistic_revision"
)
_LIST_ORGANIZATIONS_QUERY: LiteralString = (
    "SELECT organization_id, display_name, status, policy_revision, "
    "       optimistic_revision\n"
    "FROM impodo_control.organization\n"
    "WHERE (%s::text IS NULL OR (display_name, organization_id) > (%s, %s))\n"
    "ORDER BY display_name ASC, organization_id ASC\nLIMIT %s"
)
_GET_ORGANIZATION_QUERY: LiteralString = (
    "SELECT organization_id, display_name, status, policy_revision, optimistic_revision\n"
    "FROM impodo_control.organization WHERE organization_id = %s"
)
_GET_ASSIGNED_ADMINISTRATOR_QUERY: LiteralString = (
    "SELECT u.user_id, u.organization_id, u.normalized_login_email, u.display_name,\n"
    "       u.principal_kind, u.status, u.security_revision, u.optimistic_revision\n"
    "FROM impodo_control.customer_user AS u\n"
    "JOIN impodo_control.organization_administrator_assignment AS a\n"
    "  ON a.organization_id = u.organization_id AND a.user_id = u.user_id\n"
    "WHERE u.organization_id = %s AND u.principal_kind = 'ORGANIZATION_ADMINISTRATOR'"
)
_ADVANCE_CUSTOMER_REVISION_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user\n"
    "SET optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s AND user_id = %s AND optimistic_revision = %s"
)
_RENAME_ORGANIZATION_QUERY: LiteralString = (
    "UPDATE impodo_control.organization\n"
    "SET display_name = %s, optimistic_revision = optimistic_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s AND optimistic_revision = %s\n"
    "RETURNING organization_id, display_name, status, "
    "policy_revision, optimistic_revision"
)
_SET_ORGANIZATION_STATUS_QUERY: LiteralString = (
    "UPDATE impodo_control.organization\n"
    "SET status = %s, suspended_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s AND optimistic_revision = %s AND status = %s\n"
    "RETURNING organization_id, display_name, status, "
    "policy_revision, optimistic_revision"
)
_REVISE_ORGANIZATION_POLICY_QUERY: LiteralString = (
    "UPDATE impodo_control.organization\n"
    "SET policy_revision = policy_revision + 1,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s AND optimistic_revision = %s\n"
    "  AND policy_revision = %s\n"
    "RETURNING organization_id, display_name, status, "
    "policy_revision, optimistic_revision"
)
_LOCK_ORGANIZATION_QUERY: LiteralString = (
    "SELECT status, optimistic_revision\n"
    "FROM impodo_control.organization\n"
    "WHERE organization_id = %s\nFOR UPDATE"
)
_INSERT_CUSTOMER_USER_QUERY: LiteralString = (
    "INSERT INTO impodo_control.customer_user (\n"
    "    user_id, organization_id, normalized_login_email, display_name,\n"
    "    principal_kind, status, security_revision, optimistic_revision,\n"
    "    created_at, updated_at\n"
    ") VALUES (%s, %s, %s, %s, %s, 'PENDING_ACTIVATION', 1, 1, %s, %s)"
)
_INSERT_CUSTOMER_LOGIN_QUERY: LiteralString = (
    "INSERT INTO impodo_control.login_identity (\n"
    "    normalized_login_email, principal_kind, platform_administrator_id,\n"
    "    created_at, principal_id, customer_user_id\n"
    ") VALUES (%s, %s, NULL, %s, %s, %s)"
)
_INSERT_RECOVERY_TOKEN_QUERY: LiteralString = (
    "INSERT INTO impodo_control.account_recovery_token (\n"
    "    token_id, principal_kind, principal_id, purpose, token_digest, state,\n"
    "    issued_security_revision, issued_password_revision,\n"
    "    issued_by_administrator_id, issued_at, expires_at,\n"
    "    optimistic_revision, issued_by_principal_kind, issued_by_principal_id\n"
    ") VALUES (%s, %s, %s, %s, %s, 'ISSUED', %s, %s, %s, %s, %s, 1, %s, %s)"
)
_INSERT_ADMINISTRATION_EVENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.identity_administration_event (\n"
    "    event_id, deployment_id, event_kind, organization_id,\n"
    "    subject_user_id, actor_principal_kind, actor_principal_id, occurred_at\n"
    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
_INSERT_ADMINISTRATOR_ASSIGNMENT_QUERY: LiteralString = (
    "INSERT INTO impodo_control.organization_administrator_assignment (\n"
    "    organization_id, user_id, principal_kind, assignment_revision,\n"
    "    assigned_by_administrator_id, assigned_at\n"
    ") VALUES (%s, %s, 'ORGANIZATION_ADMINISTRATOR', 1, %s, %s)"
)
_LOCK_ADMINISTRATOR_ASSIGNMENT_QUERY: LiteralString = (
    "SELECT user_id, assignment_revision\n"
    "FROM impodo_control.organization_administrator_assignment\n"
    "WHERE organization_id = %s\nFOR UPDATE"
)
_REPLACE_ADMINISTRATOR_ASSIGNMENT_QUERY: LiteralString = (
    "UPDATE impodo_control.organization_administrator_assignment\n"
    "SET user_id = %s, assignment_revision = assignment_revision + 1,\n"
    "    assigned_by_administrator_id = %s, assigned_at = %s\n"
    "WHERE organization_id = %s AND assignment_revision = %s"
)
_ADVANCE_ORGANIZATION_REVISION_QUERY: LiteralString = (
    "UPDATE impodo_control.organization\n"
    "SET optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE organization_id = %s AND optimistic_revision = %s"
)
_LOAD_CUSTOMER_USER_QUERY: LiteralString = (
    "SELECT user_id, organization_id, normalized_login_email, display_name,\n"
    "       principal_kind, status, security_revision, optimistic_revision\n"
    "FROM impodo_control.customer_user\n"
    "WHERE organization_id = %s AND user_id = %s\nFOR UPDATE"
)
_LOAD_CUSTOMER_CURSOR_QUERY: LiteralString = (
    "SELECT display_name, user_id\n"
    "FROM impodo_control.customer_user\n"
    "WHERE organization_id = %s AND user_id = %s"
)
_LIST_CUSTOMER_USERS_QUERY: LiteralString = (
    "SELECT user_id, organization_id, normalized_login_email, display_name,\n"
    "       principal_kind, status, security_revision, optimistic_revision\n"
    "FROM impodo_control.customer_user\n"
    "WHERE organization_id = %s\n"
    "  AND (%s::text IS NULL OR status = %s)\n"
    "  AND (%s::text IS NULL OR (display_name, user_id) > (%s, %s))\n"
    "ORDER BY display_name ASC, user_id ASC\nLIMIT %s"
)
_GET_CUSTOMER_USER_QUERY: LiteralString = (
    "SELECT user_id, organization_id, normalized_login_email, display_name,\n"
    "       principal_kind, status, security_revision, optimistic_revision\n"
    "FROM impodo_control.customer_user\n"
    "WHERE organization_id = %s AND user_id = %s"
)
_SET_CUSTOMER_USER_STATUS_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user\n"
    "SET status = %s, security_revision = security_revision + 1,\n"
    "    optimistic_revision = optimistic_revision + 1,\n"
    "    suspended_at = CASE WHEN %s = 'SUSPENDED' THEN %s ELSE NULL END,\n"
    "    deactivated_at = CASE WHEN %s = 'DEACTIVATED' THEN %s "
    "ELSE deactivated_at END,\n"
    "    updated_at = %s\n"
    "WHERE organization_id = %s AND user_id = %s\n"
    "  AND optimistic_revision = %s AND status = ANY(%s)\n"
    "RETURNING user_id, organization_id, normalized_login_email, display_name,\n"
    "          principal_kind, status, security_revision, optimistic_revision"
)
_DEACTIVATE_REPLACED_ADMIN_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user\n"
    "SET status = 'DEACTIVATED', security_revision = security_revision + 1,\n"
    "    optimistic_revision = optimistic_revision + 1,\n"
    "    deactivated_at = %s, updated_at = %s\n"
    "WHERE organization_id = %s AND user_id = %s "
    "AND status <> 'DEACTIVATED'"
)
_REVOKE_USER_SESSIONS_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET state = 'REVOKED', revoked_at = %s, revocation_reason = %s,\n"
    "    elevated_until = NULL, optimistic_revision = optimistic_revision + 1\n"
    "WHERE organization_id = %s AND principal_id = %s AND state = 'ACTIVE'"
)
_REVOKE_ORGANIZATION_SESSIONS_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET state = 'REVOKED', revoked_at = %s, revocation_reason = %s,\n"
    "    elevated_until = NULL, optimistic_revision = optimistic_revision + 1\n"
    "WHERE organization_id = %s AND state = 'ACTIVE'"
)
_REVOKE_USER_TOKENS_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE principal_id = %s "
    "AND principal_kind IN ('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER') "
    "AND state = 'ISSUED'"
)
_LOAD_ACTIVATION_TOKEN_QUERY: LiteralString = (
    "SELECT u.user_id, u.organization_id, u.normalized_login_email,\n"
    "       u.display_name, u.principal_kind, u.status, u.security_revision,\n"
    "       u.optimistic_revision, t.optimistic_revision\n"
    "FROM impodo_control.account_recovery_token AS t\n"
    "JOIN impodo_control.customer_user AS u "
    "ON u.user_id = t.principal_id AND u.principal_kind = t.principal_kind\n"
    "JOIN impodo_control.organization AS o "
    "ON o.organization_id = u.organization_id\n"
    "WHERE t.token_id = %s AND t.token_digest = %s\n"
    "  AND t.purpose = 'ACTIVATION' AND t.state = 'ISSUED'\n"
    "  AND t.expires_at > %s AND u.status = 'PENDING_ACTIVATION'\n"
    "  AND o.status = 'ACTIVE'\nFOR UPDATE OF u, t, o"
)
_INSERT_CUSTOMER_PASSWORD_QUERY: LiteralString = (
    "INSERT INTO impodo_control.customer_user_password_identity (\n"
    "    user_id, algorithm, parameters, salt, password_hash,\n"
    "    password_revision, created_at, updated_at\n"
    ") VALUES (%s, %s, %s::jsonb, %s, %s, 1, %s, %s)"
)
_ACTIVATE_CUSTOMER_USER_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user\n"
    "SET status = 'ACTIVE', activated_at = %s, updated_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE user_id = %s AND organization_id = %s\n"
    "  AND status = 'PENDING_ACTIVATION' AND optimistic_revision = %s\n"
    "RETURNING user_id, organization_id, normalized_login_email, display_name,\n"
    "          principal_kind, status, security_revision, optimistic_revision"
)
_CONSUME_RECOVERY_TOKEN_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'CONSUMED', consumed_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE token_id = %s AND token_digest = %s AND state = 'ISSUED'\n"
    "  AND optimistic_revision = %s"
)
_LOCK_CUSTOMER_RESET_SUBJECT_QUERY: LiteralString = (
    "SELECT u.user_id, u.organization_id, u.normalized_login_email,\n"
    "       u.display_name, u.principal_kind, u.status, u.security_revision,\n"
    "       u.optimistic_revision, p.password_revision, o.status\n"
    "FROM impodo_control.customer_user AS u\n"
    "JOIN impodo_control.customer_user_password_identity AS p\n"
    "  ON p.user_id = u.user_id\n"
    "JOIN impodo_control.organization AS o\n"
    "  ON o.organization_id = u.organization_id\n"
    "WHERE u.organization_id = %s AND u.user_id = %s\n"
    "FOR UPDATE OF u, p, o"
)
_REVOKE_OUTSTANDING_RESET_QUERY: LiteralString = (
    "UPDATE impodo_control.account_recovery_token\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE principal_id = %s AND principal_kind IN "
    "('ORGANIZATION_ADMINISTRATOR', 'CUSTOMER_USER') "
    "AND purpose = 'PASSWORD_RESET' "
    "AND state = 'ISSUED'"
)
_LOAD_CUSTOMER_RESET_TOKEN_QUERY: LiteralString = (
    "SELECT u.user_id, u.organization_id, u.normalized_login_email,\n"
    "       u.display_name, u.principal_kind, u.status, u.security_revision,\n"
    "       u.optimistic_revision, p.password_revision,\n"
    "       t.issued_security_revision, t.issued_password_revision,\n"
    "       t.optimistic_revision\n"
    "FROM impodo_control.account_recovery_token AS t\n"
    "JOIN impodo_control.customer_user AS u "
    "ON u.user_id = t.principal_id AND u.principal_kind = t.principal_kind\n"
    "JOIN impodo_control.customer_user_password_identity AS p\n"
    "  ON p.user_id = u.user_id\n"
    "JOIN impodo_control.organization AS o\n"
    "  ON o.organization_id = u.organization_id\n"
    "WHERE t.token_id = %s AND t.token_digest = %s\n"
    "  AND t.purpose = 'PASSWORD_RESET' AND t.state = 'ISSUED'\n"
    "  AND t.expires_at > %s AND u.status = 'ACTIVE' AND o.status = 'ACTIVE'\n"
    "FOR UPDATE OF u, p, t, o"
)
_UPDATE_CUSTOMER_PASSWORD_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user_password_identity\n"
    "SET algorithm = %s, parameters = %s::jsonb, salt = %s,\n"
    "    password_hash = %s, password_revision = password_revision + 1,\n"
    "    updated_at = %s\n"
    "WHERE user_id = %s AND password_revision = %s"
)
_ADVANCE_CUSTOMER_SECURITY_QUERY: LiteralString = (
    "UPDATE impodo_control.customer_user\n"
    "SET security_revision = security_revision + 1,\n"
    "    optimistic_revision = optimistic_revision + 1, updated_at = %s\n"
    "WHERE user_id = %s AND security_revision = %s "
    "AND optimistic_revision = %s\n"
    "RETURNING user_id, organization_id, normalized_login_email, display_name,\n"
    "          principal_kind, status, security_revision, optimistic_revision"
)
_LIST_EVENTS_QUERY: LiteralString = (
    "SELECT event_id, event_kind, organization_id, subject_user_id,\n"
    "       actor_principal_kind, actor_principal_id, occurred_at\n"
    "FROM (\n"
    "  SELECT event_id, event_kind, organization_id, subject_user_id,\n"
    "         actor_principal_kind, actor_principal_id, occurred_at\n"
    "  FROM impodo_control.identity_administration_event WHERE organization_id = %s\n"
    "  UNION ALL\n"
    "  SELECT event_id, event_kind, organization_id, principal_id AS subject_user_id,\n"
    "         NULL::varchar AS actor_principal_kind, NULL::uuid AS actor_principal_id, occurred_at\n"
    "  FROM impodo_control.server_security_event WHERE organization_id = %s\n"
    ") AS history\n"
    "WHERE organization_id = %s\n"
    "  AND (%s::uuid IS NULL OR subject_user_id = %s)\n"
    "  AND (%s::timestamptz IS NULL OR (occurred_at, event_id) < (%s, %s))\n"
    "ORDER BY occurred_at DESC, event_id DESC\nLIMIT %s"
)
_LIST_SESSIONS_QUERY: LiteralString = (
    "SELECT session_id, principal_id, authentication_method, state, created_at,\n"
    "       last_seen_at, idle_expires_at, absolute_expires_at,\n"
    "       optimistic_revision\n"
    "FROM impodo_control.server_session\n"
    "WHERE organization_id = %s AND (%s::uuid IS NULL OR principal_id = %s)\n"
    "  AND (%s::timestamptz IS NULL OR (created_at, session_id) < (%s, %s))\n"
    "ORDER BY created_at DESC, session_id DESC\nLIMIT %s"
)
_REVOKE_SCOPED_SESSION_QUERY: LiteralString = (
    "UPDATE impodo_control.server_session\n"
    "SET state = 'REVOKED', revoked_at = %s,\n"
    "    revocation_reason = 'ADMINISTRATOR', elevated_until = NULL,\n"
    "    optimistic_revision = optimistic_revision + 1\n"
    "WHERE organization_id = %s AND session_id = %s AND state = 'ACTIVE'\n"
    "  AND optimistic_revision = %s\nRETURNING principal_id"
)


class PostgreSQLIdentityAdministrationSession(Protocol):
    """Expose one short-lived identity-administration transaction."""

    def execute(self, query: LiteralString, parameters: tuple[object, ...] = ()) -> None: ...

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int: ...

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None: ...

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class PostgreSQLIdentityAdministrationSessionFactory(Protocol):
    """Open one runtime-role administration transaction without a pool."""

    def __call__(self, dsn: str) -> PostgreSQLIdentityAdministrationSession: ...


class _PsycopgIdentityAdministrationSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def execute(self, query: LiteralString, parameters: tuple[object, ...] = ()) -> None:
        self._connection.execute(query, parameters)

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int:
        return self._connection.execute(query, parameters).rowcount

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        row = self._connection.execute(query, parameters).fetchone()
        return tuple(row) if row is not None else None

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        return tuple(
            tuple(row) for row in self._connection.execute(query, parameters).fetchall()
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class _MeasuredSession:
    def __init__(self, session: PostgreSQLIdentityAdministrationSession) -> None:
        self._session = session
        self.query_count = 0

    def execute(self, query: LiteralString, parameters: tuple[object, ...] = ()) -> None:
        self.query_count += 1
        self._session.execute(query, parameters)

    def execute_affected(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> int:
        self.query_count += 1
        return self._session.execute_affected(query, parameters)

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        self.query_count += 1
        return self._session.fetch_one(query, parameters)

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self.query_count += 1
        return self._session.fetch_rows(query, parameters)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        self._session.close()


def open_psycopg_identity_administration_session(
    dsn: str,
) -> PostgreSQLIdentityAdministrationSession:
    """Open one bounded runtime-role administration transaction."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        f"-c statement_timeout={POSTGRESQL_IDENTITY_ADMIN_STATEMENT_TIMEOUT_MILLISECONDS} "
        f"-c lock_timeout={POSTGRESQL_IDENTITY_ADMIN_LOCK_TIMEOUT_MILLISECONDS} "
        "-c idle_in_transaction_session_timeout=10000"
    )
    connection = psycopg.connect(
        conninfo=dsn,
        autocommit=False,
        connect_timeout=POSTGRESQL_IDENTITY_ADMIN_CONNECT_TIMEOUT_SECONDS,
        application_name="impodo-identity-administration",
        options=options,
    )
    return _PsycopgIdentityAdministrationSession(connection)


_Result = TypeVar("_Result")


class PsycopgIdentityAdministrationRepository:
    """Use one short transaction per tenant-scoped administration operation."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        dsn_reference: PurePosixPath,
        *,
        session_factory: PostgreSQLIdentityAdministrationSessionFactory = (
            open_psycopg_identity_administration_session
        ),
        metrics_observer: PostgreSQLTransactionMetricObserver | None = None,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._dsn_reference = dsn_reference
        self._session_factory = session_factory
        self._metrics_observer = metrics_observer

    def create_organization(
        self,
        *,
        organization_id: str,
        display_name: str,
        occurred_at: datetime,
        event: AdministrationSecurityEvent,
    ) -> OrganizationView:
        """Create one Organization after locking the active platform actor."""

        def create(session: _MeasuredSession) -> OrganizationView:
            actor = _require_actor(event)
            _authorize_actor(session, actor, organization_id=None)
            row = session.fetch_one(
                _INSERT_ORGANIZATION_QUERY,
                (organization_id, display_name, occurred_at, occurred_at),
            )
            view = _decode_organization(row)
            _insert_event(session, event, organization_id, None)
            return view

        return self._run("identity_admin.create_organization", create)

    def list_organizations(
        self,
        query: OrganizationListQuery,
    ) -> OrganizationPage:
        """Read one authorized deterministic Organization page."""

        def read(session: _MeasuredSession) -> OrganizationPage:
            _authorize_actor(session, query.actor, organization_id=None)
            rows = session.fetch_rows(
                _LIST_ORGANIZATIONS_QUERY,
                (
                    query.after_display_name,
                    query.after_display_name,
                    query.after_organization_id,
                    query.page_size + 1,
                ),
            )
            items = tuple(
                _decode_organization(row) for row in rows[: query.page_size]
            )
            has_more = len(rows) > query.page_size
            last = items[-1] if items and has_more else None
            return OrganizationPage(
                items=items,
                next_after_display_name=last.display_name if last else None,
                next_after_organization_id=(
                    last.organization_id if last else None
                ),
            )

        return self._run("identity_admin.list_organizations", read)

    def list_customer_users(
        self,
        query: CustomerUserListQuery,
    ) -> CustomerUserPage:
        """Read one authorized tenant page using an identity-only cursor."""

        def read(session: _MeasuredSession) -> CustomerUserPage:
            _authorize_actor(
                session,
                query.actor,
                organization_id=query.organization_id,
            )
            after_display_name = None
            after_user_id = None
            if query.after_user_id is not None:
                cursor = session.fetch_one(
                    _LOAD_CUSTOMER_CURSOR_QUERY,
                    (query.organization_id, query.after_user_id),
                )
                if cursor is None:
                    raise IdentityAdministrationNotFoundError(
                        "Customer account cursor is not available"
                    )
                after_display_name = _str(cursor[0])
                after_user_id = str(cursor[1])
            status = query.status.value if query.status is not None else None
            rows = session.fetch_rows(
                _LIST_CUSTOMER_USERS_QUERY,
                (
                    query.organization_id,
                    status,
                    status,
                    after_user_id,
                    after_display_name,
                    after_user_id,
                    query.page_size + 1,
                ),
            )
            items = tuple(_decode_customer(row) for row in rows[: query.page_size])
            has_more = len(rows) > query.page_size
            return CustomerUserPage(
                items=items,
                next_after_user_id=(items[-1].user_id if items and has_more else None),
            )

        return self._run("identity_admin.list_customer_users", read)

    def get_organization(self, query: OrganizationDetailQuery) -> OrganizationAdministrationView:
        """Read one provisioned Organization without customer Project access."""

        def read(session: _MeasuredSession) -> OrganizationAdministrationView:
            _authorize_actor(session, query.actor, organization_id=None)
            row = session.fetch_one(_GET_ORGANIZATION_QUERY, (query.organization_id,))
            if row is None:
                raise IdentityAdministrationNotFoundError("Organization is not available")
            administrator = session.fetch_one(_GET_ASSIGNED_ADMINISTRATOR_QUERY, (query.organization_id,))
            return OrganizationAdministrationView(
                organization=_decode_organization(row),
                administrator=_decode_customer(administrator) if administrator is not None else None,
            )

        return self._run("identity_admin.get_organization", read)

    def reissue_customer_activation(
        self, command: CustomerUserMutationCommand, issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> None:
        """Replace activation for an exact pending ordinary account atomically."""

        def reissue(session: _MeasuredSession) -> None:
            _authorize_actor(session, command.actor, organization_id=command.organization_id)
            row = session.fetch_one(_LOAD_CUSTOMER_USER_QUERY, (command.organization_id, command.user_id))
            if row is None:
                raise IdentityAdministrationNotFoundError("Customer account is not available")
            user = _decode_customer(row)
            if (
                user.principal_kind is not LoginPrincipalKind.CUSTOMER_USER
                or user.status is not CustomerUserStatus.PENDING_ACTIVATION
                or issue.purpose is not RecoveryTokenPurpose.ACTIVATION
            ):
                raise IdentityAdministrationNotFoundError("Customer account is not available")
            _advance_customer_revision(session, user, command.expected_revision, issue.issued_at)
            _replace_customer_token(session, user, issue, command.actor, None)
            _insert_event(session, event, user.organization_id, user.user_id)

        self._run("identity_admin.reissue_customer_activation", reissue)

    def issue_administrator_token(
        self, command: OrganizationMutationCommand, issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> None:
        """Issue a link for only the current assignment under the platform lock."""

        def issue_token(session: _MeasuredSession) -> None:
            _authorize_actor(session, command.actor, organization_id=None)
            organization = _require_active_organization(session, command.organization_id)
            if organization[1] != command.expected_revision:
                raise IdentityAdministrationConflictError("Organization changed")
            assignment = session.fetch_one(_LOCK_ADMINISTRATOR_ASSIGNMENT_QUERY, (command.organization_id,))
            if assignment is None:
                raise IdentityAdministrationNotFoundError("Administrator is not available")
            user_id = str(assignment[0])
            if issue.purpose is RecoveryTokenPurpose.ACTIVATION:
                row = session.fetch_one(_LOAD_CUSTOMER_USER_QUERY, (command.organization_id, user_id))
                user = _decode_customer(row)
                password_revision = None
                eligible = user.status is CustomerUserStatus.PENDING_ACTIVATION
            else:
                row = session.fetch_one(_LOCK_CUSTOMER_RESET_SUBJECT_QUERY, (command.organization_id, user_id))
                context = _decode_reset_subject(row)
                if context is None:
                    raise IdentityAdministrationNotFoundError("Administrator is not available")
                user, password_revision = context.user, context.password_revision
                eligible = user.status is CustomerUserStatus.ACTIVE
            if not eligible or user.principal_kind is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR:
                raise IdentityAdministrationNotFoundError("Administrator is not available")
            if session.execute_affected(_ADVANCE_ORGANIZATION_REVISION_QUERY, (
                issue.issued_at, command.organization_id, command.expected_revision,
            )) != 1:
                raise IdentityAdministrationConflictError("Organization changed")
            _replace_customer_token(session, user, issue, command.actor, password_revision)
            _insert_event(session, event, user.organization_id, user.user_id)

        self._run("identity_admin.issue_administrator_token", issue_token)

    def get_customer_user(
        self,
        query: CustomerUserDetailQuery,
    ) -> CustomerUserView:
        """Read one exact customer account with the tenant in the predicate."""

        def read(session: _MeasuredSession) -> CustomerUserView:
            _authorize_actor(
                session,
                query.actor,
                organization_id=query.organization_id,
            )
            row = session.fetch_one(
                _GET_CUSTOMER_USER_QUERY,
                (query.organization_id, query.user_id),
            )
            if row is None:
                raise IdentityAdministrationNotFoundError(
                    "Customer account is not available"
                )
            return _decode_customer(row)

        return self._run("identity_admin.get_customer_user", read)

    def rename_organization(
        self,
        command: RenameOrganizationCommand,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView:
        """Rename one exact Organization under the platform actor lock."""

        def rename(session: _MeasuredSession) -> OrganizationView:
            _authorize_actor(session, command.mutation.actor, organization_id=None)
            row = session.fetch_one(
                _RENAME_ORGANIZATION_QUERY,
                (
                    command.display_name,
                    occurred_at,
                    command.mutation.organization_id,
                    command.mutation.expected_revision,
                ),
            )
            view = _organization_or_conflict(row)
            _insert_event(session, event, view.organization_id, None)
            return view

        return self._run("identity_admin.rename_organization", rename)

    def set_organization_status(
        self,
        command: OrganizationMutationCommand,
        status: OrganizationStatus,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView:
        """Transition one Organization and revoke its active sessions on suspend."""

        def transition(session: _MeasuredSession) -> OrganizationView:
            _authorize_actor(session, command.actor, organization_id=None)
            previous = (
                OrganizationStatus.ACTIVE
                if status is OrganizationStatus.SUSPENDED
                else OrganizationStatus.SUSPENDED
            )
            row = session.fetch_one(
                _SET_ORGANIZATION_STATUS_QUERY,
                (
                    status.value,
                    occurred_at if status is OrganizationStatus.SUSPENDED else None,
                    occurred_at,
                    command.organization_id,
                    command.expected_revision,
                    previous.value,
                ),
            )
            view = _organization_or_conflict(row)
            if status is OrganizationStatus.SUSPENDED:
                session.execute(
                    _REVOKE_ORGANIZATION_SESSIONS_QUERY,
                    (
                        occurred_at,
                        "ORGANIZATION_SUSPENDED",
                        command.organization_id,
                    ),
                )
            _insert_event(session, event, command.organization_id, None)
            return view

        return self._run("identity_admin.set_organization_status", transition)

    def revise_organization_policy(
        self,
        command: ReviseOrganizationPolicyCommand,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView:
        """Advance one policy revision and revoke existing Organization sessions."""

        def revise(session: _MeasuredSession) -> OrganizationView:
            _authorize_actor(session, command.mutation.actor, organization_id=None)
            row = session.fetch_one(
                _REVISE_ORGANIZATION_POLICY_QUERY,
                (
                    occurred_at,
                    command.mutation.organization_id,
                    command.mutation.expected_revision,
                    command.expected_policy_revision,
                ),
            )
            view = _organization_or_conflict(row)
            session.execute(
                _REVOKE_ORGANIZATION_SESSIONS_QUERY,
                (
                    occurred_at,
                    "SECURITY_REVISION",
                    command.mutation.organization_id,
                ),
            )
            _insert_event(session, event, command.mutation.organization_id, None)
            return view

        return self._run("identity_admin.revise_organization_policy", revise)

    def create_customer_user(
        self,
        record: CustomerUserRecord,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView:
        """Create one pending user, login identity, and activation token atomically."""

        def create(session: _MeasuredSession) -> CustomerUserView:
            actor = _require_actor(event)
            _authorize_actor(session, actor, organization_id=record.organization_id)
            _require_active_organization(session, record.organization_id)
            _persist_customer(session, record, issue, actor)
            if record.principal_kind is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR:
                session.execute(
                    _INSERT_ADMINISTRATOR_ASSIGNMENT_QUERY,
                    (
                        record.organization_id,
                        record.user_id,
                        actor.principal_id,
                        record.created_at,
                    ),
                )
            _insert_event(session, event, record.organization_id, record.user_id)
            return _customer_view(record)

        return self._run("identity_admin.create_customer_user", create)

    def replace_organization_administrator(
        self,
        command: ReplaceOrganizationAdministratorCommand,
        record: CustomerUserRecord,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView:
        """Deactivate the old assignment and create its pending replacement."""

        def replace(session: _MeasuredSession) -> CustomerUserView:
            _authorize_actor(session, command.actor, organization_id=None)
            organization = _require_active_organization(session, command.organization_id)
            if organization[1] != command.expected_organization_revision:
                raise IdentityAdministrationConflictError("Organization changed")
            assignment = session.fetch_one(
                _LOCK_ADMINISTRATOR_ASSIGNMENT_QUERY,
                (command.organization_id,),
            )
            _persist_customer(session, record, issue, command.actor)
            if assignment is None:
                session.execute(
                    _INSERT_ADMINISTRATOR_ASSIGNMENT_QUERY,
                    (
                        command.organization_id,
                        record.user_id,
                        command.actor.principal_id,
                        record.created_at,
                    ),
                )
                event_kind = AdministrationEventKind.ORGANIZATION_ADMINISTRATOR_ASSIGNED
            else:
                old_user_id, assignment_revision = str(assignment[0]), _int(assignment[1])
                session.execute(
                    _DEACTIVATE_REPLACED_ADMIN_QUERY,
                    (
                        record.created_at,
                        record.created_at,
                        command.organization_id,
                        old_user_id,
                    ),
                )
                _revoke_user_access(
                    session,
                    organization_id=command.organization_id,
                    user_id=old_user_id,
                    occurred_at=record.created_at,
                    reason="ACCOUNT_SUSPENDED",
                )
                if (
                    session.execute_affected(
                        _REPLACE_ADMINISTRATOR_ASSIGNMENT_QUERY,
                        (
                            record.user_id,
                            command.actor.principal_id,
                            record.created_at,
                            command.organization_id,
                            assignment_revision,
                        ),
                    )
                    != 1
                ):
                    raise IdentityAdministrationConflictError("Assignment changed")
                event_kind = AdministrationEventKind.ORGANIZATION_ADMINISTRATOR_REPLACED
            if (
                session.execute_affected(
                    _ADVANCE_ORGANIZATION_REVISION_QUERY,
                    (
                        record.created_at,
                        command.organization_id,
                        command.expected_organization_revision,
                    ),
                )
                != 1
            ):
                raise IdentityAdministrationConflictError("Organization changed")
            _insert_event(
                session,
                event,
                record.organization_id,
                record.user_id,
                kind=event_kind,
            )
            return _customer_view(record)

        return self._run("identity_admin.replace_organization_administrator", replace)

    def activate_customer_user(
        self,
        mutation: CustomerCredentialMutation,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView | None:
        """Consume activation with the initial password and active transition."""

        def activate(session: _MeasuredSession) -> CustomerUserView | None:
            row = session.fetch_one(
                _LOAD_ACTIVATION_TOKEN_QUERY,
                (
                    mutation.proof.token_id,
                    mutation.proof.digest,
                    mutation.occurred_at,
                ),
            )
            context = _decode_activation_context(row)
            if context is None:
                return None
            user, token_revision = context
            password = mutation.password_hash
            session.execute(
                _INSERT_CUSTOMER_PASSWORD_QUERY,
                (
                    user.user_id,
                    password.algorithm,
                    _password_parameters(password.parameters),
                    password.salt,
                    password.digest,
                    mutation.occurred_at,
                    mutation.occurred_at,
                ),
            )
            activated = session.fetch_one(
                _ACTIVATE_CUSTOMER_USER_QUERY,
                (
                    mutation.occurred_at,
                    mutation.occurred_at,
                    user.user_id,
                    user.organization_id,
                    user.optimistic_revision,
                ),
            )
            if activated is None or session.execute_affected(
                _CONSUME_RECOVERY_TOKEN_QUERY,
                (
                    mutation.occurred_at,
                    mutation.proof.token_id,
                    mutation.proof.digest,
                    token_revision,
                ),
            ) != 1:
                raise IdentityAdministrationConflictError("Activation changed")
            session.execute(
                _REVOKE_USER_TOKENS_QUERY,
                (mutation.occurred_at, user.user_id),
            )
            view = _decode_customer(activated)
            _insert_event(session, event, view.organization_id, view.user_id)
            return view

        return self._run("identity_admin.activate_customer_user", activate)

    def set_customer_user_status(
        self,
        command: CustomerUserMutationCommand,
        status: CustomerUserStatus,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> CustomerUserView:
        """Transition one scoped customer account and revoke its active access."""

        def transition(session: _MeasuredSession) -> CustomerUserView:
            _authorize_actor(session, command.actor, organization_id=command.organization_id)
            _require_active_organization(session, command.organization_id)
            current_row = session.fetch_one(
                _LOAD_CUSTOMER_USER_QUERY,
                (command.organization_id, command.user_id),
            )
            if current_row is None:
                raise IdentityAdministrationNotFoundError(
                    "Customer account is not available"
                )
            current = _decode_customer(current_row)
            if (
                command.actor.principal_kind
                is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
                and current.principal_kind
                is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            ):
                raise IdentityAdministrationNotFoundError("Customer account is not available")
            if current.principal_kind is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR:
                assignment = session.fetch_one(
                    _LOCK_ADMINISTRATOR_ASSIGNMENT_QUERY,
                    (command.organization_id,),
                )
                if assignment is not None and str(assignment[0]) == current.user_id:
                    raise IdentityAdministrationConflictError(
                        "Assigned administrator must be replaced"
                    )
            allowed = _allowed_customer_statuses(status)
            row = session.fetch_one(
                _SET_CUSTOMER_USER_STATUS_QUERY,
                (
                    status.value,
                    status.value,
                    occurred_at,
                    status.value,
                    occurred_at,
                    occurred_at,
                    command.organization_id,
                    command.user_id,
                    command.expected_revision,
                    [value.value for value in allowed],
                ),
            )
            view = _customer_or_conflict(row)
            if status in {CustomerUserStatus.SUSPENDED, CustomerUserStatus.DEACTIVATED}:
                _revoke_user_access(
                    session,
                    organization_id=command.organization_id,
                    user_id=command.user_id,
                    occurred_at=occurred_at,
                    reason="ACCOUNT_SUSPENDED",
                )
            _insert_event(session, event, command.organization_id, command.user_id)
            return view

        return self._run("identity_admin.set_customer_user_status", transition)

    def issue_customer_reset(
        self,
        command: CustomerUserMutationCommand,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> None:
        """Replace one outstanding reset token under subject and actor locks."""

        def issue_reset(session: _MeasuredSession) -> None:
            _authorize_actor(session, command.actor, organization_id=command.organization_id)
            row = session.fetch_one(
                _LOCK_CUSTOMER_RESET_SUBJECT_QUERY,
                (command.organization_id, command.user_id),
            )
            context = _decode_reset_subject(row)
            if (
                context is None
                or context.user.status is not CustomerUserStatus.ACTIVE
                or context.user.principal_kind is not LoginPrincipalKind.CUSTOMER_USER
                or context.organization_status is not OrganizationStatus.ACTIVE
                or context.user.optimistic_revision != command.expected_revision
            ):
                raise IdentityAdministrationNotFoundError("Customer account is not available")
            session.execute(
                _REVOKE_OUTSTANDING_RESET_QUERY,
                (issue.issued_at, context.user.user_id),
            )
            _advance_customer_revision(session, context.user, command.expected_revision, issue.issued_at)
            _insert_recovery_token(
                session,
                context.user,
                issue,
                command.actor,
                password_revision=context.password_revision,
            )
            _insert_event(
                session,
                event,
                command.organization_id,
                command.user_id,
            )

        self._run("identity_admin.issue_customer_reset", issue_reset)

    def reset_customer_password(
        self,
        mutation: CustomerCredentialMutation,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView | None:
        """Consume reset with password, revision, token, and session changes."""

        def reset(session: _MeasuredSession) -> CustomerUserView | None:
            row = session.fetch_one(
                _LOAD_CUSTOMER_RESET_TOKEN_QUERY,
                (
                    mutation.proof.token_id,
                    mutation.proof.digest,
                    mutation.occurred_at,
                ),
            )
            context = _decode_reset_token(row)
            if context is None:
                return None
            user, password_revision, issued_security, issued_password, token_revision = context
            if (
                user.security_revision != issued_security
                or password_revision != issued_password
            ):
                return None
            password = mutation.password_hash
            if session.execute_affected(
                _UPDATE_CUSTOMER_PASSWORD_QUERY,
                (
                    password.algorithm,
                    _password_parameters(password.parameters),
                    password.salt,
                    password.digest,
                    mutation.occurred_at,
                    user.user_id,
                    password_revision,
                ),
            ) != 1:
                raise IdentityAdministrationConflictError("Customer password changed")
            updated = session.fetch_one(
                _ADVANCE_CUSTOMER_SECURITY_QUERY,
                (
                    mutation.occurred_at,
                    user.user_id,
                    user.security_revision,
                    user.optimistic_revision,
                ),
            )
            if updated is None or session.execute_affected(
                _CONSUME_RECOVERY_TOKEN_QUERY,
                (
                    mutation.occurred_at,
                    mutation.proof.token_id,
                    mutation.proof.digest,
                    token_revision,
                ),
            ) != 1:
                raise IdentityAdministrationConflictError("Customer reset changed")
            session.execute(
                _REVOKE_USER_TOKENS_QUERY,
                (mutation.occurred_at, user.user_id),
            )
            session.execute(
                _REVOKE_USER_SESSIONS_QUERY,
                (
                    mutation.occurred_at,
                    "PASSWORD_RESET",
                    user.organization_id,
                    user.user_id,
                ),
            )
            view = _decode_customer(updated)
            _insert_event(session, event, view.organization_id, view.user_id)
            return view

        return self._run("identity_admin.reset_customer_password", reset)

    def list_security_events(
        self,
        query: AdministrationEventQuery,
    ) -> AdministrationEventPage:
        """Read one tenant-scoped newest-first security-event page."""

        def read(session: _MeasuredSession) -> AdministrationEventPage:
            _authorize_actor(session, query.actor, organization_id=query.organization_id)
            rows = session.fetch_rows(
                _LIST_EVENTS_QUERY,
                (
                    query.organization_id,
                    query.organization_id,
                    query.organization_id,
                    query.user_id,
                    query.user_id,
                    query.before_occurred_at,
                    query.before_occurred_at,
                    query.before_event_id,
                    query.page_size + 1,
                ),
            )
            items = tuple(_decode_event(row) for row in rows[: query.page_size])
            has_more = len(rows) > query.page_size
            last = items[-1] if items and has_more else None
            return AdministrationEventPage(
                items=items,
                next_before_occurred_at=last.occurred_at if last else None,
                next_before_event_id=last.event_id if last else None,
            )

        return self._run("identity_admin.list_security_events", read)

    def list_sessions(
        self,
        query: OrganizationSessionQuery,
    ) -> OrganizationSessionPage:
        """Read one tenant-scoped newest-first session page."""

        def read(session: _MeasuredSession) -> OrganizationSessionPage:
            _authorize_actor(session, query.actor, organization_id=query.organization_id)
            rows = session.fetch_rows(
                _LIST_SESSIONS_QUERY,
                (
                    query.organization_id,
                    query.user_id,
                    query.user_id,
                    query.before_created_at,
                    query.before_created_at,
                    query.before_session_id,
                    query.page_size + 1,
                ),
            )
            items = tuple(_decode_session(row) for row in rows[: query.page_size])
            has_more = len(rows) > query.page_size
            last = items[-1] if items and has_more else None
            return OrganizationSessionPage(
                items=items,
                next_before_created_at=last.created_at if last else None,
                next_before_session_id=last.session_id if last else None,
            )

        return self._run("identity_admin.list_sessions", read)

    def revoke_session(
        self,
        command: RevokeOrganizationSessionCommand,
        event: AdministrationSecurityEvent,
        *,
        revoked_at: datetime,
    ) -> None:
        """Revoke one exact session with Organization scope in the predicate."""

        def revoke(session: _MeasuredSession) -> None:
            _authorize_actor(session, command.actor, organization_id=command.organization_id)
            row = session.fetch_one(
                _REVOKE_SCOPED_SESSION_QUERY,
                (
                    revoked_at,
                    command.organization_id,
                    command.session_id,
                    command.expected_revision,
                ),
            )
            if row is None:
                raise IdentityAdministrationNotFoundError("Session is not available")
            _insert_event(session, event, command.organization_id, str(row[0]))

        self._run("identity_admin.revoke_session", revoke)

    def _run(
        self,
        operation_name: str,
        operation: Callable[[_MeasuredSession], _Result],
    ) -> _Result:
        try:
            dsn = self._dsn_reader(self._dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, psycopg.Error) as error:
            raise IdentityAdministrationUnavailableError(
                "Identity administration is unavailable"
            ) from error
        measured = _MeasuredSession(session)
        started = perf_counter()
        outcome = PostgreSQLTransactionOutcome.FAILED
        try:
            try:
                result = operation(measured)
                measured.commit()
                outcome = PostgreSQLTransactionOutcome.COMMITTED
                return result
            except IdentityAdministrationError:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise
            except psycopg.errors.UniqueViolation as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise IdentityAdministrationConflictError(
                    "Identity administration conflicts with existing state"
                ) from error
            except (
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled,
                psycopg.errors.SerializationFailure,
                psycopg.InterfaceError,
                psycopg.OperationalError,
            ) as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise IdentityAdministrationUnavailableError(
                    "Identity administration is unavailable"
                ) from error
            except psycopg.Error as error:
                _rollback_or_unavailable(measured)
                outcome = PostgreSQLTransactionOutcome.ROLLED_BACK
                raise IdentityAdministrationStorageError(
                    "Identity administration statement failed"
                ) from error
        finally:
            active_error = sys.exception()
            duration_milliseconds = (perf_counter() - started) * 1000
            try:
                measured.close()
            except psycopg.Error as error:
                if active_error is None:
                    raise IdentityAdministrationUnavailableError(
                        "Identity administration is unavailable"
                    ) from error
            finally:
                report_postgresql_transaction_metric(
                    self._metrics_observer,
                    PostgreSQLTransactionMetric(
                        operation=operation_name,
                        outcome=outcome,
                        connection_count=POSTGRESQL_IDENTITY_ADMIN_CONNECTION_BUDGET,
                        query_count=measured.query_count,
                        pool_wait_milliseconds=0.0,
                        transaction_duration_milliseconds=duration_milliseconds,
                        retry_count=0,
                    ),
                )


class _ResetSubject:
    def __init__(
        self,
        user: CustomerUserView,
        password_revision: int,
        organization_status: OrganizationStatus,
    ) -> None:
        self.user = user
        self.password_revision = password_revision
        self.organization_status = organization_status


def _advance_customer_revision(
    session: _MeasuredSession, user: CustomerUserView,
    expected_revision: int, occurred_at: datetime,
) -> None:
    if session.execute_affected(_ADVANCE_CUSTOMER_REVISION_QUERY, (
        occurred_at, user.organization_id, user.user_id, expected_revision,
    )) != 1:
        raise IdentityAdministrationConflictError("Customer account changed")


def _replace_customer_token(
    session: _MeasuredSession, user: CustomerUserView, issue: RecoveryTokenIssue,
    actor: AdministrationActor, password_revision: int | None,
) -> None:
    session.execute(_REVOKE_USER_TOKENS_QUERY, (issue.issued_at, user.user_id))
    _insert_recovery_token(session, user, issue, actor, password_revision=password_revision)


def _authorize_actor(
    session: _MeasuredSession,
    actor: AdministrationActor,
    *,
    organization_id: str | None,
) -> None:
    if actor.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
        row = session.fetch_one(_LOCK_PLATFORM_ACTOR_QUERY, (actor.principal_id,))
        if row is None or PlatformAdministratorStatus(_str(row[0])) is not PlatformAdministratorStatus.ACTIVE:
            raise IdentityAdministrationNotFoundError("Administration actor is not available")
        return
    if organization_id is None or actor.organization_id != organization_id:
        raise IdentityAdministrationNotFoundError("Administration target is not available")
    row = session.fetch_one(
        _LOCK_ORGANIZATION_ACTOR_QUERY,
        (actor.principal_id, organization_id),
    )
    if row is None or (
        CustomerUserStatus(_str(row[0])) is not CustomerUserStatus.ACTIVE
        or OrganizationStatus(_str(row[1])) is not OrganizationStatus.ACTIVE
    ):
        raise IdentityAdministrationNotFoundError("Administration actor is not available")


def _require_active_organization(
    session: _MeasuredSession,
    organization_id: str,
) -> tuple[OrganizationStatus, int]:
    row = session.fetch_one(_LOCK_ORGANIZATION_QUERY, (organization_id,))
    if row is None:
        raise IdentityAdministrationNotFoundError("Organization is not available")
    status = OrganizationStatus(_str(row[0]))
    if status is not OrganizationStatus.ACTIVE:
        raise IdentityAdministrationConflictError("Organization is not active")
    return status, _int(row[1])


def _persist_customer(
    session: _MeasuredSession,
    record: CustomerUserRecord,
    issue: RecoveryTokenIssue,
    actor: AdministrationActor,
) -> None:
    session.execute(
        _INSERT_CUSTOMER_USER_QUERY,
        (
            record.user_id,
            record.organization_id,
            record.normalized_login_email,
            record.display_name,
            record.principal_kind.value,
            record.created_at,
            record.created_at,
        ),
    )
    session.execute(
        _INSERT_CUSTOMER_LOGIN_QUERY,
        (
            record.normalized_login_email,
            record.principal_kind.value,
            record.created_at,
            record.user_id,
            record.user_id,
        ),
    )
    _insert_recovery_token(session, _customer_view(record), issue, actor, password_revision=None)


def _insert_recovery_token(
    session: _MeasuredSession,
    user: CustomerUserView,
    issue: RecoveryTokenIssue,
    actor: AdministrationActor,
    *,
    password_revision: int | None,
) -> None:
    session.execute(
        _INSERT_RECOVERY_TOKEN_QUERY,
        (
            issue.token_id,
            user.principal_kind.value,
            user.user_id,
            issue.purpose.value,
            issue.digest,
            user.security_revision,
            password_revision,
            (
                actor.principal_id
                if actor.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR
                else None
            ),
            issue.issued_at,
            issue.expires_at,
            actor.principal_kind.value,
            actor.principal_id,
        ),
    )


def _insert_event(
    session: _MeasuredSession,
    event: AdministrationSecurityEvent,
    organization_id: str,
    subject_user_id: str | None,
    *,
    kind: AdministrationEventKind | None = None,
) -> None:
    session.execute(
        _INSERT_ADMINISTRATION_EVENT_QUERY,
        (
            event.event_id,
            event.deployment_id,
            (kind or event.kind).value,
            organization_id,
            subject_user_id,
            event.actor.principal_kind.value if event.actor else None,
            event.actor.principal_id if event.actor else None,
            event.occurred_at,
        ),
    )


def _revoke_user_access(
    session: _MeasuredSession,
    *,
    organization_id: str,
    user_id: str,
    occurred_at: datetime,
    reason: str,
) -> None:
    session.execute(
        _REVOKE_USER_SESSIONS_QUERY,
        (occurred_at, reason, organization_id, user_id),
    )
    session.execute(_REVOKE_USER_TOKENS_QUERY, (occurred_at, user_id))


def _organization_or_conflict(row: tuple[object, ...] | None) -> OrganizationView:
    if row is None:
        raise IdentityAdministrationConflictError("Organization changed")
    return _decode_organization(row)


def _customer_or_conflict(row: tuple[object, ...] | None) -> CustomerUserView:
    if row is None:
        raise IdentityAdministrationConflictError("Customer account changed")
    return _decode_customer(row)


def _decode_organization(row: tuple[object, ...] | None) -> OrganizationView:
    if row is None or len(row) != 5:
        raise IdentityAdministrationStorageError("Organization result is invalid")
    try:
        return OrganizationView(
            organization_id=_str(row[0]),
            display_name=_str(row[1]),
            status=OrganizationStatus(_str(row[2])),
            policy_revision=_int(row[3]),
            optimistic_revision=_int(row[4]),
        )
    except (TypeError, ValueError) as error:
        raise IdentityAdministrationStorageError("Organization result is invalid") from error


def _decode_customer(row: tuple[object, ...] | None) -> CustomerUserView:
    if row is None or len(row) < 8:
        raise IdentityAdministrationStorageError("Customer account result is invalid")
    try:
        return CustomerUserView(
            user_id=str(row[0]),
            organization_id=_str(row[1]),
            normalized_login_email=_str(row[2]),
            display_name=_str(row[3]),
            principal_kind=LoginPrincipalKind(_str(row[4])),
            status=CustomerUserStatus(_str(row[5])),
            security_revision=_int(row[6]),
            optimistic_revision=_int(row[7]),
        )
    except (TypeError, ValueError) as error:
        raise IdentityAdministrationStorageError(
            "Customer account result is invalid"
        ) from error


def _decode_activation_context(
    row: tuple[object, ...] | None,
) -> tuple[CustomerUserView, int] | None:
    if row is None:
        return None
    if len(row) != 9:
        raise IdentityAdministrationStorageError("Activation result is invalid")
    return _decode_customer(row[:8]), _int(row[8])


def _decode_reset_subject(row: tuple[object, ...] | None) -> _ResetSubject | None:
    if row is None:
        return None
    if len(row) != 10:
        raise IdentityAdministrationStorageError("Reset subject is invalid")
    return _ResetSubject(
        _decode_customer(row[:8]),
        _int(row[8]),
        OrganizationStatus(_str(row[9])),
    )


def _decode_reset_token(
    row: tuple[object, ...] | None,
) -> tuple[CustomerUserView, int, int, int, int] | None:
    if row is None:
        return None
    if len(row) != 12:
        raise IdentityAdministrationStorageError("Reset token is invalid")
    return (
        _decode_customer(row[:8]),
        _int(row[8]),
        _int(row[9]),
        _int(row[10]),
        _int(row[11]),
    )


def _decode_event(row: tuple[object, ...]) -> AdministrationEventView:
    if len(row) != 7:
        raise IdentityAdministrationStorageError("Administration event is invalid")
    actor_kind = None if row[4] is None else LoginPrincipalKind(_str(row[4]))
    return AdministrationEventView(
        event_id=str(row[0]),
        kind=AdministrationEventKind(_str(row[1])),
        organization_id=_str(row[2]),
        subject_user_id=None if row[3] is None else str(row[3]),
        actor_kind=actor_kind,
        actor_id=None if row[5] is None else str(row[5]),
        occurred_at=_datetime(row[6]),
    )


def _decode_session(row: tuple[object, ...]) -> OrganizationSessionView:
    if len(row) != 9:
        raise IdentityAdministrationStorageError("Organization session is invalid")
    return OrganizationSessionView(
        session_id=str(row[0]),
        user_id=str(row[1]),
        authentication_method=_str(row[2]),
        state=_str(row[3]),
        created_at=_datetime(row[4]),
        last_seen_at=_datetime(row[5]),
        idle_expires_at=_datetime(row[6]),
        absolute_expires_at=_datetime(row[7]),
        optimistic_revision=_int(row[8]),
    )


def _customer_view(record: CustomerUserRecord) -> CustomerUserView:
    return CustomerUserView(
        user_id=record.user_id,
        organization_id=record.organization_id,
        normalized_login_email=record.normalized_login_email,
        display_name=record.display_name,
        principal_kind=record.principal_kind,
        status=record.status,
        security_revision=record.security_revision,
        optimistic_revision=record.optimistic_revision,
    )


def _allowed_customer_statuses(
    target: CustomerUserStatus,
) -> tuple[CustomerUserStatus, ...]:
    if target is CustomerUserStatus.SUSPENDED:
        return (CustomerUserStatus.ACTIVE,)
    if target is CustomerUserStatus.ACTIVE:
        return (CustomerUserStatus.SUSPENDED,)
    if target is CustomerUserStatus.DEACTIVATED:
        return (
            CustomerUserStatus.PENDING_ACTIVATION,
            CustomerUserStatus.ACTIVE,
            CustomerUserStatus.SUSPENDED,
        )
    raise IdentityAdministrationConflictError("Customer account transition is invalid")


def _require_actor(event: AdministrationSecurityEvent) -> AdministrationActor:
    if event.actor is None:
        raise IdentityAdministrationStorageError("Administration event actor is absent")
    return event.actor


def _password_parameters(parameters: tuple[tuple[str, int], ...]) -> str:
    return json.dumps(dict(parameters), separators=(",", ":"), sort_keys=True)


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError
    return value


def _rollback_or_unavailable(
    session: PostgreSQLIdentityAdministrationSession,
) -> None:
    try:
        session.rollback()
    except psycopg.Error as error:
        raise IdentityAdministrationUnavailableError(
            "Identity administration is unavailable"
        ) from error


__all__ = [
    "POSTGRESQL_IDENTITY_ADMIN_CONNECTION_BUDGET",
    "PostgreSQLIdentityAdministrationSession",
    "PostgreSQLIdentityAdministrationSessionFactory",
    "PsycopgIdentityAdministrationRepository",
    "open_psycopg_identity_administration_session",
]
