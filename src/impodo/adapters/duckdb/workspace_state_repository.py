"""Persist mutable mapping-engine state inside one MigrationWorkspace."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil

import duckdb

from ...access import Actor
from ...workspace_state import (
    WorkspaceState,
    WorkspaceStateConflictError,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
    SourceFile,
)
from .repository import DuckDbRepository
from .serialization import _project_from_rows, _project_values


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

    def create_unlinked(self, workspace: WorkspaceState, *, actor: Actor) -> None:
        """Initialize one standalone workspace engine without a business registry.

        The browser uses ``MigrationWorkspaceStateRepository``, whose override
        first verifies the owning Project and MigrationWorkspace. This base
        operation remains useful for isolated repository tests and tools that
        deliberately exercise only the workspace-engine boundary.
        """

        directory = self.workspace_directory(workspace.project_id)
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
                self._insert_project(connection, workspace)
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
        return self._get_project_unresolved(workspace_id)

    def assert_workspace_mutable(self, workspace_id: str) -> None:
        """Reject changes to a locally closed workspace-engine state."""

        if self.get(workspace_id).status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("This MigrationWorkspace is closed and read-only")


    def has_audit_event(self, project_id: str, event_type: str) -> bool:
        """Return whether the project recorded the exact lifecycle event."""

        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
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
        project_id: str,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        """Append a credential event without mutating project semantics."""

        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
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
        project: WorkspaceState,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        """Save one optimistic lifecycle change and invalidate affected evidence.

        Target changes retire current schema/mapping/staging evidence;
        ownership or retention changes retire quality evidence. Successful
        registration also refreshes the portable registration manifest.
        """

        database_path = self.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT revision, odoo_connection_mode, odoo_base_url,
                           odoo_database, intended_applications,
                           intended_models, data_manager,
                           functional_owner, retention_days,
                           data_classification
                      FROM workspace_state
                    """
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                target_changed = event_type == "WORKSPACE_TARGET_UPDATED" and (
                    str(current[1] or "")
                    != (
                        project.odoo_connection_mode.value
                        if project.odoo_connection_mode
                        else ""
                    )
                    or str(current[2]) != project.odoo_base_url
                    or str(current[3]) != project.odoo_database
                    or tuple(json.loads(str(current[4])))
                    != project.intended_applications
                    or tuple(json.loads(str(current[5])))
                    != project.intended_models
                )
                governance_changed = (
                    str(current[6]) != project.data_manager
                    or str(current[7]) != project.functional_owner
                    or int(current[8]) != project.retention_days
                    or str(current[9]) != project.data_classification.value
                )
                self._update_project(connection, project)
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
                    project,
                    event_type=event_type,
                    detail=event_detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if project.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(project)

    def add_source_file(
        self,
        project: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Insert one immutable source row without rewriting existing evidence."""

        database_path = self.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM workspace_state"
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
                self._update_project(connection, project)
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
                    project,
                    event_type="SOURCE_FILE_ADDED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if project.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(project)


    def remove_source_file(
        self,
        project: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Atomically remove one unfrozen file and its file-scoped evidence."""

        database_path = self.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM workspace_state"
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
                self._update_project(connection, project)
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
                    project,
                    event_type="SOURCE_FILE_REMOVED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if project.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(project)

    def update_schema_scope(
        self,
        project: WorkspaceState,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace Stage C's model allowlist and invalidate its dependents."""

        database_path = self.workspace_directory(project.project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM workspace_state"
                ).fetchone()
                if current is None:
                    raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
                if current[0] != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The workspace was modified by another request"
                    )
                self._update_project(connection, project)
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
                    project,
                    event_type="SCHEMA_SCOPE_UPDATED",
                    detail=(
                        f"{len(project.intended_models)} permitted model(s); "
                        "captured schema and active mapping invalidated"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if project.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(project)

    def synchronize_registration_artifacts(self, project_id: str) -> None:
        """Refresh registry and manifest after another repository updates status."""

        project = self.get(project_id)
        if project.status is WorkspaceStatus.REGISTERED:
            self._write_registration_manifest(project)


    def _get_project_unresolved(self, project_id: str) -> WorkspaceState:
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            row = connection.execute("SELECT * FROM workspace_state").fetchone()
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
        return _project_from_rows(
            dict(zip(columns, row, strict=True)),
            source_rows,
        )

    def _insert_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: WorkspaceState,
    ) -> None:
        connection.execute(
            f"INSERT INTO workspace_state VALUES ({', '.join('?' for _ in range(26))})",
            _project_values(project),
        )
    def _update_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: WorkspaceState,
    ) -> None:
        connection.execute(
            """
            UPDATE workspace_state SET
                name = ?,
                source_system = ?,
                source_mode = ?,
                export_status = ?,
                export_date = ?,
                description = ?,
                data_manager = ?,
                functional_owner = ?,
                business_unit = ?,
                data_classification = ?,
                retention_days = ?,
                support_access = ?,
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
            WHERE project_id = ?
            """,
            _project_values(project)[1:] + [project.project_id],
        )
    def _write_registration_manifest(self, project: WorkspaceState) -> Path:
        payload = {
            "contract_version": 4,
            "project": {
                "project_id": project.project_id,
                "name": project.name,
                "source_system": project.source_system,
                "source_mode": project.source_mode.value,
                "export_status": project.export_status.value,
                "export_date": (
                    project.export_date.isoformat() if project.export_date else None
                ),
                "data_manager": project.data_manager,
                "functional_owner": project.functional_owner,
                "business_unit": project.business_unit,
                "data_classification": project.data_classification.value,
                "retention_days": project.retention_days,
                "support_access": project.support_access,
                "odoo_connection_mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "odoo_base_url": project.odoo_base_url,
                "odoo_database": project.odoo_database,
                "intended_applications": list(project.intended_applications),
                "intended_models": list(project.intended_models),
                "status": project.status.value,
                "revision": project.revision,
                "created_at": project.created_at.isoformat(),
                "registered_at": (
                    project.registered_at.isoformat()
                    if project.registered_at
                    else None
                ),
                "mapping_version": project.mapping_version,
                "current_run_id": project.current_run_id,
                "approval_status": project.approval_status.value,
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
                for source_file in project.source_files
            ],
        }
        audit_dir = self.workspace_directory(project.project_id) / "audit"
        target = audit_dir / (
            f"project-registration-r{project.revision}.json"
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

