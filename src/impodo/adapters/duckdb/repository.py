"""Infrastructure shared by the concrete DuckDB repositories."""

from __future__ import annotations

from pathlib import Path
import duckdb

from ...access import Actor
from .database import DuckDbDatabase
from .unit_of_work import DuckDbUnitOfWork


class DuckDbRepository:
    """Give one concrete repository access to the shared database boundary."""

    def __init__(self, database: DuckDbDatabase) -> None:
        self._database = database

    @property
    def root(self) -> Path:
        return self._database.root

    @property
    def registry_path(self) -> Path:
        return self._database.registry_path

    @property
    def _transformation_impact_lock(self):
        return self._database._transformation_impact_lock

    def project_directory(self, project_id: str) -> Path:
        return self._database.project_directory(project_id)

    def unit_of_work(self, project_id: str) -> DuckDbUnitOfWork:
        return self._database.unit_of_work(project_id)

    def _connect(self, path: Path):
        return self._database._connect(path)

    def _read_json_rows(
        self,
        project_id: str,
        query: str,
        parameters: list[object] | None = None,
    ) -> tuple[str, ...]:
        return self._database._read_json_rows(project_id, query, parameters)

    def _read_singleton_json(self, project_id: str, query: str) -> str | None:
        return self._database._read_singleton_json(project_id, query)

    def _save_singleton(
        self,
        project_id: str,
        *,
        table: str,
        value_column: str,
        value: str,
        event_type: str,
        detail: str,
        actor: Actor,
        invalidate: tuple[str, ...] = (),
    ) -> None:
        self._database._save_singleton(
            project_id,
            table=table,
            value_column=value_column,
            value=value,
            event_type=event_type,
            detail=detail,
            actor=actor,
            invalidate=invalidate,
        )

    def _initialize_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
        *args,
        **kwargs,
    ) -> None:
        self._database._initialize_project_database(connection, *args, **kwargs)

    def _migrate_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        self._database._migrate_project_database(connection)

    def _insert_workspace_audit(
        self,
        connection: duckdb.DuckDBPyConnection,
        **kwargs,
    ) -> None:
        self._database._insert_workspace_audit(connection, **kwargs)

    def _insert_audit(
        self,
        connection: duckdb.DuckDBPyConnection,
        *args,
        **kwargs,
    ) -> None:
        self._database._insert_audit(connection, *args, **kwargs)

    def _invalidate_normalization(
        self,
        connection: duckdb.DuckDBPyConnection,
        **kwargs,
    ) -> None:
        self._database._invalidate_normalization(connection, **kwargs)

    def _invalidate_quality(
        self,
        connection: duckdb.DuckDBPyConnection,
        **kwargs,
    ) -> None:
        self._database._invalidate_quality(connection, **kwargs)

    def _invalidate_canonical_staging(
        self,
        connection: duckdb.DuckDBPyConnection,
        **kwargs,
    ) -> None:
        self._database._invalidate_canonical_staging(connection, **kwargs)

    @staticmethod
    def _project_revision(connection: duckdb.DuckDBPyConnection) -> int:
        return DuckDbDatabase._project_revision(connection)
