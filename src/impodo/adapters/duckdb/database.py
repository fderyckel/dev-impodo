"""DuckDB database implementation."""

from __future__ import annotations

from .constants import (
    NORMALIZATION_ROW_BATCH_SIZE,
    QUALITY_ROW_BATCH_SIZE,
    SCHEMA_VERSION,
    STAGING_ROW_BATCH_SIZE,
    TRANSFORMATION_IMPACT_ROW_BATCH_SIZE,
)

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator
from uuid import UUID

import duckdb

from ...access import Actor
from ...projects import ProjectNotFoundError





from .audit import AuditMixin
from .invalidation import EvidenceInvalidationMixin
from .migrations.project import ProjectMigrationsMixin
from .migrations.registry import ensure_registry_schema
from .unit_of_work import (
    DuckDbConnectionFactory,
    DuckDbUnitOfWork,
)


class DuckDbDatabase(
    ProjectMigrationsMixin, EvidenceInvalidationMixin, AuditMixin
):
    """Shared DuckDB connection, migration, and transaction boundary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.connection_factory = DuckDbConnectionFactory()
        self._transformation_impact_lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.duckdb"
        with self._connect(self.registry_path) as connection:
            ensure_registry_schema(connection)

    def project_directory(self, project_id: str) -> Path:
        try:
            canonical = str(UUID(project_id))
        except (ValueError, AttributeError) as error:
            raise ProjectNotFoundError("Invalid project identifier") from error
        candidate = self.root / canonical
        target = candidate.resolve()
        if target != candidate or target.parent != self.root:
            raise ProjectNotFoundError("Invalid project identifier")
        return target

    def unit_of_work(self, project_id: str) -> DuckDbUnitOfWork:
        """Return one project-scoped transaction shared by collaborating ports."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        return DuckDbUnitOfWork(
            self.connection_factory,
            database_path,
            prepare=self._migrate_project_database,
        )

    def _read_json_rows(
        self,
        project_id: str,
        query: str,
        parameters: list[object] | None = None,
    ) -> tuple[str, ...]:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            rows = connection.execute(query, parameters or []).fetchall()
        return tuple(str(row[0]) for row in rows)
    def _read_singleton_json(
        self,
        project_id: str,
        query: str,
    ) -> str | None:
        values = self._read_json_rows(project_id, query)
        return values[0] if values else None
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
        permitted = {
            ("source_selection", "selection_json"),
            ("odoo_model_catalog", "catalog_json"),
            ("odoo_schema_catalog", "catalog_json"),
        }
        if (table, value_column) not in permitted:
            raise ValueError("Unsupported workspace table")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {table} (singleton_id, {value_column})
                    VALUES (1, ?)
                    """,
                    [value],
                )
                for target in invalidate:
                    if target not in {
                        "derived_entity_plan_current",
                        "mapping_current",
                        "schema_governance_current",
                    }:
                        raise ValueError("Unsupported invalidation table")
                    connection.execute(f"DELETE FROM {target}")
                if table in {"source_selection", "odoo_schema_catalog"}:
                    self._invalidate_canonical_staging(
                        connection,
                        reason=(
                            "SOURCE_SELECTION_CHANGED"
                            if table == "source_selection"
                            else "ODOO_SCHEMA_CHANGED"
                        ),
                    )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type=event_type,
                    detail=detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    @staticmethod
    def _project_revision(connection: duckdb.DuckDBPyConnection) -> int:
        row = connection.execute("SELECT revision FROM project").fetchone()
        if row is None:
            raise ProjectNotFoundError("Project not found")
        return int(row[0])
    @contextmanager
    def _connect(self, path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.connection_factory.connect(path) as connection:
            yield connection
