"""Infrastructure shared by the concrete DuckDB repositories."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
import duckdb

from ...access import Actor
from ...workspace_state import WorkspaceState
from .database import DuckDbWorkspaceDatabase
from .unit_of_work import DuckDbUnitOfWork


class WorkspaceAggregateReader(Protocol):
    """Load policy and source-file context from one workspace."""

    def get(self, project_id: str) -> WorkspaceState: ...


class DuckDbRepository:
    """Give concrete repositories the one shared infrastructure boundary.

    These methods deliberately forward rather than add business decisions.
    Repository subclasses own SQL/evidence semantics; the database owns
    connection policy, schema preparation, audit, invalidation, and unit-of-work
    setup.
    """

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        self._database = database

    @property
    def root(self) -> Path:
        """Return the secured root shared by database and artifact adapters."""

        return self._database.root

    @property
    def _transformation_impact_lock(self):
        return self._database._transformation_impact_lock

    def workspace_directory(self, workspace_id: str) -> Path:
        """Delegate contained workspace-directory validation to the database."""

        return self._database.workspace_directory(workspace_id)

    def unit_of_work(self, workspace_id: str) -> DuckDbUnitOfWork:
        """Return a workspace transaction reusable by collaborating repositories."""

        return self._database.unit_of_work(workspace_id)

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

    def _initialize_workspace_database(
        self,
        connection: duckdb.DuckDBPyConnection,
        *args,
        **kwargs,
    ) -> None:
        self._database._initialize_workspace_database(connection, *args, **kwargs)

    def _ensure_workspace_database_schema(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        self._database._ensure_workspace_database_schema(connection)

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

    def _invalidate_resolution(
        self,
        connection: duckdb.DuckDBPyConnection,
        **kwargs,
    ) -> None:
        self._database._invalidate_resolution(connection, **kwargs)

    @staticmethod
    def _workspace_revision(connection: duckdb.DuckDBPyConnection) -> int:
        return DuckDbWorkspaceDatabase._workspace_revision(connection)

