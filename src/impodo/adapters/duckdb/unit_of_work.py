"""Hardened DuckDB connections and explicit project transaction scopes.

The factory disables extension loading and external access and bounds memory
and threads. ``DuckDbUnitOfWork`` prepares the schema before beginning,
commits only on a clean context exit, rolls back on every exception, and
always closes the short-lived connection.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
import time
from typing import Callable, Iterator

import duckdb

from ...workspace_errors import WorkspaceDatabaseBusyError


DUCKDB_CONFIG = {
    "allow_community_extensions": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "enable_external_access": "false",
    "lock_configuration": "true",
    "memory_limit": "256MB",
    "threads": "2",
}

_LOCK_CONTENTION_MARKERS = (
    "being used by another process",
    "could not set lock on file",
    "conflicting lock is held",
)
_DATABASE_BUSY_MESSAGE = (
    "Another Impodo task is still using this project's saved data. "
    "No Odoo records were changed and your previous prepared evidence remains "
    "available. Wait a moment, close any other Impodo tabs editing this project, "
    "then try again."
)


class DuckDbConnectionFactory:
    """Open consistently hardened short-lived DuckDB connections."""

    def __init__(
        self,
        *,
        lock_wait_timeout_seconds: float = 0.0,
        lock_retry_interval_seconds: float = 0.05,
    ) -> None:
        if lock_wait_timeout_seconds < 0:
            raise ValueError("lock_wait_timeout_seconds cannot be negative")
        if lock_retry_interval_seconds <= 0:
            raise ValueError("lock_retry_interval_seconds must be positive")
        self.lock_wait_timeout_seconds = lock_wait_timeout_seconds
        self.lock_retry_interval_seconds = lock_retry_interval_seconds

    @contextmanager
    def connect(
        self,
        path: Path,
        *,
        memory_limit: str | None = None,
        threads: str | None = None,
        preserve_insertion_order: bool | None = None,
        enable_external_access: bool | None = None,
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield one hardened connection and close it on every exit path."""

        config = dict(DUCKDB_CONFIG)
        if memory_limit is not None:
            config["memory_limit"] = memory_limit
        if threads is not None:
            config["threads"] = threads
        if preserve_insertion_order is not None:
            config["preserve_insertion_order"] = str(
                preserve_insertion_order
            ).casefold()
        if enable_external_access is not None:
            config["enable_external_access"] = str(enable_external_access).casefold()
        connection = self._connect_with_lock_wait(path, config)
        try:
            yield connection
        finally:
            connection.close()

    def _connect_with_lock_wait(
        self,
        path: Path,
        config: dict[str, str],
    ) -> duckdb.DuckDBPyConnection:
        """Wait only for a transient cross-process DuckDB file lock."""

        deadline = time.monotonic() + self.lock_wait_timeout_seconds
        while True:
            try:
                return duckdb.connect(str(path), config=config)
            except duckdb.IOException as error:
                if not _is_lock_contention(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceDatabaseBusyError(
                        _DATABASE_BUSY_MESSAGE
                    ) from error
                time.sleep(min(self.lock_retry_interval_seconds, remaining))


def _is_lock_contention(error: duckdb.IOException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in _LOCK_CONTENTION_MARKERS)


class DuckDbUnitOfWork(AbstractContextManager["DuckDbUnitOfWork"]):
    """Own one explicit transaction shared by a project-scoped command.

    Services can pass this boundary to cooperating repository operations when
    several evidence writes and pointer changes must succeed atomically.
    """

    def __init__(
        self,
        factory: DuckDbConnectionFactory,
        path: Path,
        *,
        prepare: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
    ) -> None:
        self._factory = factory
        self._path = path
        self._prepare = prepare
        self._connection_context = None
        self.connection: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> "DuckDbUnitOfWork":
        self._connection_context = self._factory.connect(self._path)
        self.connection = self._connection_context.__enter__()
        if self._prepare is not None:
            self._prepare(self.connection)
        self.connection.begin()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self.connection is None or self._connection_context is None:
            return False
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self._connection_context.__exit__(exc_type, exc_value, traceback)
            self.connection = None
            self._connection_context = None
        return False
