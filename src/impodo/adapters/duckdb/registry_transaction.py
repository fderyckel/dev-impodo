"""Coordinate one atomic mutation of the shared migration registry."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import duckdb

from .migration_foundation_database import MigrationFoundationDatabase


class RegistryTransactionCoordinator:
    """Open, commit, or roll back exactly one registry transaction.

    Root repositories share the migration registry. This coordinator owns only
    the DuckDB transaction boundary; individual record collaborators retain
    their aggregate validation, event, and operation-intent responsibilities.
    """

    def __init__(self, database: MigrationFoundationDatabase) -> None:
        self._database = database

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield one registry connection that commits only on normal return."""

        with self._database.connect(self._database.registry_path) as connection:
            connection.begin()
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
