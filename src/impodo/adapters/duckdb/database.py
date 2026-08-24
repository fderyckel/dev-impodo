"""Shared hardened DuckDB boundary for workspace-engine databases.

``DuckDbWorkspaceDatabase`` owns connection configuration, schema preparation,
workspace paths, transaction factories, audit helpers, and downstream
invalidation. Concrete repositories are thin responsibility-specific adapters
over this shared boundary; they do not open differently configured databases.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator
from uuid import UUID

import duckdb

from ...access import Actor
from ...workspace_state import WorkspaceStateNotFoundError
from .audit import AuditMixin
from .invalidation import EvidenceInvalidationMixin
from .schema.workspace_engine import WorkspaceEngineSchemaMixin
from .unit_of_work import (
    DuckDbConnectionFactory,
    DuckDbUnitOfWork,
)


class DuckDbWorkspaceDatabase(
    WorkspaceEngineSchemaMixin, EvidenceInvalidationMixin, AuditMixin
):
    """Workspace-scoped DuckDB connection, schema, and transaction boundary.

    The root contains UUID-named workspace directories and engine databases. DuckDB
    external access and extension loading are disabled by the connection
    factory. Workspace access requires the exact current schema. This boundary
    deliberately knows nothing about the Project foundation store, so a
    spawned worker can operate on one workspace without contending for
    ``registry.duckdb``.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        lock_wait_timeout_seconds: float = 0.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.connection_factory = DuckDbConnectionFactory(
            lock_wait_timeout_seconds=lock_wait_timeout_seconds
        )
        self._transformation_impact_lock = RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def workspace_directory(self, workspace_id: str) -> Path:
        """Return the contained UUID directory for a validated workspace ID."""

        try:
            canonical = str(UUID(workspace_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceStateNotFoundError("Invalid workspace identifier") from error
        candidate = self.root / canonical
        target = candidate.resolve()
        if target != candidate or target.parent != self.root:
            raise WorkspaceStateNotFoundError("Invalid workspace identifier")
        return target

    def unit_of_work(self, workspace_id: str) -> DuckDbUnitOfWork:
        """Return one workspace-scoped transaction shared by collaborating ports."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        return DuckDbUnitOfWork(
            self.connection_factory,
            database_path,
            prepare=self._ensure_workspace_database_schema,
        )

    def _read_json_rows(
        self,
        workspace_id: str,
        query: str,
        parameters: list[object] | None = None,
    ) -> tuple[str, ...]:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            rows = connection.execute(query, parameters or []).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _read_singleton_json(
        self,
        workspace_id: str,
        query: str,
    ) -> str | None:
        values = self._read_json_rows(workspace_id, query)
        return values[0] if values else None

    def _save_singleton(
        self,
        workspace_id: str,
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
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            revision = self._workspace_revision(connection)
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
                        "odoo_capture_selection_current",
                        "odoo_capture_manifest_current",
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
    def _workspace_revision(connection: duckdb.DuckDBPyConnection) -> int:
        row = connection.execute(
            "SELECT revision FROM workspace_projection_cache"
        ).fetchone()
        if row is None:
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        return int(row[0])

    @contextmanager
    def _connect(self, path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.connection_factory.connect(path) as connection:
            yield connection

