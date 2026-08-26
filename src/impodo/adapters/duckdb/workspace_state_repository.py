"""Persist mutable mapping-engine state inside one MigrationWorkspace."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil

import duckdb

from impodo.domain.shared.access import Actor
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateConflictError,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
    SourceFile,
)
from .repository import DuckDbRepository
from .serialization import _workspace_from_rows, _workspace_values


class WorkspaceStateRepository(DuckDbRepository):
    """Own mutable workspace state and its downstream invalidation."""

    _ENGINE_DIRECTORIES = (
        "inbox",
        "staging",
        "snapshots",
        "protected",
        "reports",
        "audit",
    )

    def initialize_workbench(
        self,
        workspace: WorkspaceState,
        *,
        actor: Actor,
    ) -> None:
        """Initialize one isolated mapping workbench engine.

        The browser uses ``MigrationWorkspaceStateRepository``, whose override
        first verifies the owning Project and MigrationWorkspace. This base
        operation remains useful for isolated repository tests and tools that
        deliberately exercise only the contained workbench boundary.
        """

        directory = self.workspace_directory(workspace.workspace_id)
        database_path = directory / "workspace-engine.duckdb"
        if database_path.is_file():
            raise WorkspaceStateError("MigrationWorkspace engine already exists")
        created_root = False
        created: list[Path] = []
        try:
            directory.mkdir(exist_ok=False)
            created_root = True
            for name in self._ENGINE_DIRECTORIES:
                child = directory / name
                child.mkdir()
                created.append(child)
            (directory / "protected").chmod(0o700)
            with self._connect(database_path) as connection:
                self._initialize_workspace_database(connection)
                self._insert_workspace(connection, workspace)
                self._insert_audit(
                    connection,
                    workspace,
                    event_type="MIGRATION_WORKSPACE_ENGINE_CREATED",
                    detail="",
                    actor=actor,
                )
        except Exception:
            database_path.unlink(missing_ok=True)
            if created_root:
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def get(self, workspace_id: str) -> WorkspaceState:
        """Return current mutable state from one initialized workspace engine."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace engine not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
        return self._get_workspace_unresolved(workspace_id)

    def assert_workspace_mutable(self, workspace_id: str) -> None:
        """Reject changes to a locally closed workspace-engine state."""

        if self.get(workspace_id).status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("This MigrationWorkspace is closed and read-only")


    def has_audit_event(self, workspace_id: str, event_type: str) -> bool:
        """Return whether the workspace recorded the exact lifecycle event."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM audit_event
                     WHERE event_type = ?
                )
                """,
                [event_type],
            ).fetchone()
        return bool(row and row[0])

    def record_credential_event(
        self,
        workspace_id: str,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        """Append a credential event without mutating workspace semantics."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type=event_type,
                    detail=detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


    def save(
        self,
        workspace: WorkspaceState,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        """Save one optimistic lifecycle change and invalidate affected evidence.

        Target changes retire current schema/mapping/staging evidence.
        Successful registration also refreshes the portable registration
        manifest.
        """

        database_path = self.workspace_directory(workspace.workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT revision, data_classification, retention_days,
                           odoo_connection_mode, odoo_base_url,
                           odoo_database, intended_applications,
                           intended_models
                      FROM workspace_projection_cache
                    """
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                target_changed = event_type == "WORKSPACE_TARGET_UPDATED" and (
                    str(current[3] or "")
                    != (
                        workspace.odoo_connection_mode.value
                        if workspace.odoo_connection_mode
                        else ""
                    )
                    or str(current[4]) != workspace.odoo_base_url
                    or str(current[5]) != workspace.odoo_database
                    or tuple(json.loads(str(current[6])))
                    != workspace.intended_applications
                    or tuple(json.loads(str(current[7])))
                    != workspace.intended_models
                )
                governance_changed = (
                    str(current[1]) != workspace.data_classification.value
                    or int(current[2]) != workspace.retention_days
                )
                self._update_workspace(connection, workspace)
                if target_changed:
                    connection.execute("DELETE FROM odoo_schema_catalog")
                    connection.execute(
                        "DELETE FROM odoo_capture_selection_current"
                    )
                    connection.execute("DELETE FROM odoo_capture_manifest_current")
                    connection.execute("DELETE FROM schema_governance_current")
                    connection.execute("DELETE FROM mapping_current")
                    connection.execute("DELETE FROM supporting_lookup_current")
                    self._invalidate_canonical_staging(
                        connection,
                        reason="WORKSPACE_TARGET_CHANGED",
                    )
                elif governance_changed:
                    self._invalidate_quality(
                        connection,
                        reason="WORKSPACE_GOVERNANCE_CHANGED",
                    )
                self._insert_audit(
                    connection,
                    workspace,
                    event_type=event_type,
                    detail=event_detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if workspace.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(workspace)

    def add_source_file(
        self,
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Insert one immutable source row without rewriting existing evidence."""

        database_path = self.workspace_directory(workspace.workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM workspace_projection_cache"
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                if str(current[1]) == WorkspaceStatus.CLOSED.value:
                    raise WorkspaceStateError("Closed workspaces cannot be edited")
                if connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM source_selection)"
                ).fetchone()[0]:
                    raise WorkspaceStateError(
                        "Source files cannot be changed after table choices are saved"
                    )
                self._update_workspace(connection, workspace)
                self._invalidate_canonical_staging(
                    connection,
                    reason="SOURCE_FILE_ADDED",
                )
                connection.execute(
                    """
                    INSERT INTO source_file VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        source_file.file_id,
                        source_file.display_name,
                        source_file.stored_name,
                        source_file.size_bytes,
                        source_file.sha256,
                        source_file.received_at.isoformat(),
                    ],
                )
                self._insert_audit(
                    connection,
                    workspace,
                    event_type="SOURCE_FILE_ADDED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if workspace.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(workspace)


    def remove_source_file(
        self,
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Atomically remove one unfrozen file and its file-scoped evidence."""

        database_path = self.workspace_directory(workspace.workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM workspace_projection_cache"
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                if str(current[1]) == WorkspaceStatus.CLOSED.value:
                    raise WorkspaceStateError("Closed workspaces cannot be edited")
                if connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM source_selection)"
                ).fetchone()[0]:
                    raise WorkspaceStateError(
                        "Source files cannot be changed after table choices are saved"
                    )
                stored = connection.execute(
                    "SELECT stored_name FROM source_file WHERE file_id = ?",
                    [source_file.file_id],
                ).fetchone()
                if stored is None or str(stored[0]) != source_file.stored_name:
                    raise WorkspaceStateError(
                        "The selected source file is no longer in this workspace"
                    )
                self._update_workspace(connection, workspace)
                connection.execute(
                    "DELETE FROM source_configuration WHERE file_id = ?",
                    [source_file.file_id],
                )
                connection.execute(
                    "DELETE FROM source_catalog WHERE file_id = ?",
                    [source_file.file_id],
                )
                connection.execute(
                    "DELETE FROM source_file WHERE file_id = ?",
                    [source_file.file_id],
                )
                self._insert_audit(
                    connection,
                    workspace,
                    event_type="SOURCE_FILE_REMOVED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if workspace.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(workspace)

    def update_schema_scope(
        self,
        workspace: WorkspaceState,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace Stage C's model allowlist and invalidate its dependents."""

        database_path = self.workspace_directory(workspace.workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM workspace_projection_cache"
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                self._update_workspace(connection, workspace)
                connection.execute("DELETE FROM odoo_schema_catalog")
                connection.execute("DELETE FROM odoo_capture_selection_current")
                connection.execute("DELETE FROM odoo_capture_manifest_current")
                connection.execute("DELETE FROM schema_governance_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SCHEMA_SCOPE_CHANGED",
                )
                self._insert_audit(
                    connection,
                    workspace,
                    event_type="SCHEMA_SCOPE_UPDATED",
                    detail=(
                        f"{len(workspace.intended_models)} permitted model(s); "
                        "captured schema and active mapping invalidated"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if workspace.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(workspace)

    def synchronize_registration_artifacts(self, workspace_id: str) -> None:
        """Refresh registry and manifest after another repository updates status."""

        workspace = self.get(workspace_id)
        if workspace.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(workspace)


    def _get_workspace_unresolved(self, workspace_id: str) -> WorkspaceState:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            row = connection.execute(
                "SELECT * FROM workspace_projection_cache"
            ).fetchone()
            if row is None:
                raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
            columns = [item[0] for item in connection.description]
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _workspace_from_rows(
            dict(zip(columns, row, strict=True)),
            source_rows,
            workspace_id=workspace_id,
        )

    def _insert_workspace(
        self,
        connection: duckdb.DuckDBPyConnection,
        workspace: WorkspaceState,
    ) -> None:
        connection.execute(
            f"INSERT INTO workspace_projection_cache VALUES "
            f"({', '.join('?' for _ in range(19))})",
            _workspace_values(workspace),
        )
    def _update_workspace(
        self,
        connection: duckdb.DuckDBPyConnection,
        workspace: WorkspaceState,
    ) -> None:
        connection.execute(
            """
            UPDATE workspace_projection_cache SET
                name = ?,
                source_system = ?,
                source_mode = ?,
                data_classification = ?,
                retention_days = ?,
                odoo_connection_mode = ?,
                odoo_base_url = ?,
                odoo_database = ?,
                intended_applications = ?,
                intended_models = ?,
                status = ?,
                revision = ?,
                created_at = ?,
                updated_at = ?,
                registered_at = ?,
                mapping_version = ?,
                current_run_id = ?,
                approval_status = ?
            WHERE singleton_id = 1
            """,
            _workspace_values(workspace)[1:],
        )
    def _write_registration_manifest(self, workspace: WorkspaceState) -> Path:
        payload = {
            "contract_version": 6,
            "workspace": {
                "workspace_id": workspace.workspace_id,
                "name": workspace.name,
                "source_system": workspace.source_system,
                "source_mode": workspace.source_mode.value,
                "data_classification": workspace.data_classification.value,
                "retention_days": workspace.retention_days,
                "odoo_connection_mode": (
                    workspace.odoo_connection_mode.value
                    if workspace.odoo_connection_mode
                    else None
                ),
                "odoo_base_url": workspace.odoo_base_url,
                "odoo_database": workspace.odoo_database,
                "intended_applications": list(workspace.intended_applications),
                "intended_models": list(workspace.intended_models),
                "status": workspace.status.value,
                "revision": workspace.revision,
                "created_at": workspace.created_at.isoformat(),
                "registered_at": (
                    workspace.registered_at.isoformat()
                    if workspace.registered_at
                    else None
                ),
                "mapping_version": workspace.mapping_version,
                "current_run_id": workspace.current_run_id,
                "approval_status": workspace.approval_status.value,
            },
            "source_files": [
                {
                    "file_id": source_file.file_id,
                    "display_name": source_file.display_name,
                    "stored_name": source_file.stored_name,
                    "size_bytes": source_file.size_bytes,
                    "sha256": source_file.sha256,
                    "received_at": source_file.received_at.isoformat(),
                }
                for source_file in workspace.source_files
            ],
        }
        audit_dir = self.workspace_directory(workspace.workspace_id) / "audit"
        target = audit_dir / (
            f"workspace-registration-r{workspace.revision}.json"
        )
        partial = target.with_suffix(".json.partial")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        partial.write_bytes(encoded)
        partial.replace(target)
        return target
