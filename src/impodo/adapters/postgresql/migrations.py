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


_MIGRATION_0001_STATEMENTS = (
    PostgreSQLMigrationStatement(_CREATE_CONTRACT_QUERY),
    PostgreSQLMigrationStatement(
        _INSERT_CONTRACT_QUERY,
        (
            "control",
            POSTGRESQL_CONTROL_SCHEMA_GENERATION,
            POSTGRESQL_CONTROL_SCHEMA_VERSION,
        ),
    ),
    PostgreSQLMigrationStatement(_GRANT_SCHEMA_QUERY),
    PostgreSQLMigrationStatement(_GRANT_CONTRACT_QUERY),
)

POSTGRESQL_MIGRATIONS = (
    PostgreSQLMigrationDefinition(
        version=1,
        name="create-control-schema-contract",
        checksum="24c4c383ecaaa0626766b8eae24cc17009e69179044a038d556a3ac9a445b556",
        statements=_MIGRATION_0001_STATEMENTS,
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
