"""Inspect PostgreSQL reachability and schema through one bounded session."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, LiteralString, Protocol

import psycopg

from impodo.adapters.postgresql.control_schema import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
    POSTGRESQL_RUNTIME_ROLE,
)

POSTGRESQL_CONNECT_TIMEOUT_SECONDS = 5
POSTGRESQL_STATEMENT_TIMEOUT_MILLISECONDS = 5_000

_REACHABILITY_QUERY: LiteralString = "SELECT 1, current_user"
_SCHEMA_QUERY: LiteralString = (
    "SELECT generation, version\n"
    "FROM impodo_control.schema_contract\n"
    "WHERE contract_name = %s\n"
    "ORDER BY contract_name\n"
    "LIMIT 2"
)


class PostgreSQLDsnError(RuntimeError):
    """Report that the DSN reference could not be resolved safely."""


class PostgreSQLTransportError(RuntimeError):
    """Report a bounded connection or transport failure."""


class PostgreSQLStatementError(RuntimeError):
    """Report that PostgreSQL rejected a bounded startup statement."""


class PostgreSQLUnavailableError(RuntimeError):
    """Report failed reachability without exposing connection detail."""


class PostgreSQLSchemaCompatibilityError(RuntimeError):
    """Report an absent, old, new, or malformed control schema."""


class PostgreSQLDsnReader(Protocol):
    """Resolve a DSN reference without retaining or logging its value."""

    def __call__(self, reference: PurePosixPath) -> str: ...


class PostgreSQLStartupSession(Protocol):
    """Expose the bounded query surface required during startup."""

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]: ...

    def close(self) -> None: ...


class PostgreSQLStartupSessionFactory(Protocol):
    """Open one short-lived, read-only PostgreSQL startup session."""

    def __call__(self, dsn: str) -> PostgreSQLStartupSession: ...


class _PsycopgStartupSession:
    def __init__(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        self._connection = connection

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        try:
            rows = self._connection.execute(query, parameters).fetchall()
        except (psycopg.OperationalError, psycopg.InterfaceError) as error:
            raise PostgreSQLTransportError(
                "PostgreSQL startup transport failed"
            ) from error
        except psycopg.Error as error:
            raise PostgreSQLStatementError(
                "PostgreSQL startup statement failed"
            ) from error
        return tuple(tuple(row) for row in rows)

    def close(self) -> None:
        try:
            self._connection.close()
        except psycopg.Error as error:
            raise PostgreSQLTransportError(
                "PostgreSQL startup connection close failed"
            ) from error


def open_psycopg_startup_session(dsn: str) -> PostgreSQLStartupSession:
    """Open one autocommit session with server-enforced read-only limits."""

    options = (
        f"-c role={POSTGRESQL_RUNTIME_ROLE} "
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={POSTGRESQL_STATEMENT_TIMEOUT_MILLISECONDS} "
        "-c lock_timeout=1000"
    )
    try:
        connection = psycopg.connect(
            conninfo=dsn,
            autocommit=True,
            connect_timeout=POSTGRESQL_CONNECT_TIMEOUT_SECONDS,
            application_name="impodo-startup",
            options=options,
        )
    except psycopg.Error as error:
        raise PostgreSQLTransportError(
            "PostgreSQL startup connection failed"
        ) from error
    return _PsycopgStartupSession(connection)


@dataclass(frozen=True, slots=True)
class PostgreSQLSchemaContract:
    """Name the exact control-plane generation accepted by this build."""

    generation: str = POSTGRESQL_CONTROL_SCHEMA_GENERATION
    version: int = POSTGRESQL_CONTROL_SCHEMA_VERSION


_DEFAULT_SCHEMA_CONTRACT = PostgreSQLSchemaContract()


class PsycopgPostgreSQLStartupInspector:
    """Inspect reachability and schema without retaining a connection or DSN."""

    def __init__(
        self,
        dsn_reader: PostgreSQLDsnReader,
        *,
        session_factory: PostgreSQLStartupSessionFactory = (
            open_psycopg_startup_session
        ),
        schema_contract: PostgreSQLSchemaContract = _DEFAULT_SCHEMA_CONTRACT,
    ) -> None:
        self._dsn_reader = dsn_reader
        self._session_factory = session_factory
        self._schema_contract = schema_contract

    def inspect(self, dsn_reference: PurePosixPath) -> None:
        """Prove reachability and exact schema compatibility in one session."""

        try:
            dsn = self._dsn_reader(dsn_reference)
            session = self._session_factory(dsn)
        except (PostgreSQLDsnError, PostgreSQLTransportError) as error:
            raise PostgreSQLUnavailableError(
                "PostgreSQL startup reachability failed"
            ) from error

        try:
            try:
                reachability_rows = session.fetch_rows(_REACHABILITY_QUERY)
            except (PostgreSQLTransportError, PostgreSQLStatementError) as error:
                raise PostgreSQLUnavailableError(
                    "PostgreSQL startup reachability failed"
                ) from error
            if reachability_rows != ((1, POSTGRESQL_RUNTIME_ROLE),):
                raise PostgreSQLUnavailableError(
                    "PostgreSQL startup reachability failed"
                )

            try:
                schema_rows = session.fetch_rows(
                    _SCHEMA_QUERY,
                    ("control",),
                )
            except PostgreSQLTransportError as error:
                raise PostgreSQLUnavailableError(
                    "PostgreSQL startup reachability failed"
                ) from error
            except PostgreSQLStatementError as error:
                raise PostgreSQLSchemaCompatibilityError(
                    "PostgreSQL control schema is incompatible"
                ) from error

            expected = (
                self._schema_contract.generation,
                self._schema_contract.version,
            )
            if schema_rows != (expected,):
                raise PostgreSQLSchemaCompatibilityError(
                    "PostgreSQL control schema is incompatible"
                )
        finally:
            active_error = sys.exception()
            try:
                session.close()
            except PostgreSQLTransportError as error:
                if active_error is None:
                    raise PostgreSQLUnavailableError(
                        "PostgreSQL startup reachability failed"
                    ) from error


__all__ = [
    "POSTGRESQL_CONNECT_TIMEOUT_SECONDS",
    "POSTGRESQL_CONTROL_SCHEMA_GENERATION",
    "POSTGRESQL_CONTROL_SCHEMA_VERSION",
    "POSTGRESQL_STATEMENT_TIMEOUT_MILLISECONDS",
    "PostgreSQLDsnError",
    "PostgreSQLDsnReader",
    "PostgreSQLSchemaCompatibilityError",
    "PostgreSQLSchemaContract",
    "PostgreSQLStartupSession",
    "PostgreSQLStartupSessionFactory",
    "PostgreSQLStatementError",
    "PostgreSQLTransportError",
    "PostgreSQLUnavailableError",
    "PsycopgPostgreSQLStartupInspector",
    "open_psycopg_startup_session",
]
