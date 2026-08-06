"""Hardened DuckDB connections and explicit project transaction scopes.

The factory disables extension loading and external access and bounds memory
and threads. ``DuckDbUnitOfWork`` migrates before beginning, commits only on a
clean context exit, rolls back on every exception, and always closes the
short-lived connection.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Callable, Iterator

import duckdb


DUCKDB_CONFIG = {
    "allow_community_extensions": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "enable_external_access": "false",
    "lock_configuration": "true",
    "memory_limit": "256MB",
    "threads": "2",
}


class DuckDbConnectionFactory:
    """Open consistently hardened short-lived DuckDB connections."""

    @contextmanager
    def connect(
        self,
        path: Path,
        *,
        memory_limit: str | None = None,
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield one hardened connection and close it on every exit path."""

        config = dict(DUCKDB_CONFIG)
        if memory_limit is not None:
            config["memory_limit"] = memory_limit
        connection = duckdb.connect(str(path), config=config)
        try:
            yield connection
        finally:
            connection.close()


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
