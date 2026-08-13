"""Persist Stage A project state, registry rows, files, and audit evidence.

Layer: DuckDB/filesystem adapter. ``ProjectRepository`` implements the port
used by ``ProjectService``. It owns optimistic transactions, the per-project
directory/database boundary, lightweight registry synchronization, registration
manifests, and downstream invalidation caused by project-level changes.

See ``docs/architecture/python-code-map.md`` and ``tests/test_projects.py``.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
from uuid import uuid4

import duckdb

from ...access import Actor
from ...projects import (
    MigrationProject,
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectSummary,
    SourceFile,
)
from .database import DuckDbDatabase
from .repository import DuckDbRepository





from .serialization import (
    _project_from_rows,
    _project_values,
)


class ProjectRepository(DuckDbRepository):
    """Own durable project state and project-level invalidation transactions."""

    def __init__(self, database: DuckDbDatabase) -> None:
        super().__init__(database)
        self._recover_pending_registry_sync()

    def create(self, project: MigrationProject, *, actor: Actor) -> None:
        """Create the contained project directory, database, audit, and registry row."""

        self._mark_registry_sync_pending(project.project_id)
        project_dir = self.project_directory(project.project_id)
        project_dir.mkdir(parents=False, exist_ok=False)
        for child in (
            "inbox",
            "staging",
            "snapshots",
            "protected",
            "reports",
            "audit",
        ):
            (project_dir / child).mkdir()
        (project_dir / "protected").chmod(0o700)
        database_path = project_dir / "project.duckdb"
        with self._connect(database_path) as connection:
            self._initialize_project_database(connection)
            self._insert_project(connection, project)
            self._insert_audit(
                connection,
                project,
                event_type="PROJECT_CREATED",
                detail="",
                actor=actor,
            )
        self._update_registry(project)
    def get(self, project_id: str) -> MigrationProject:
        """Load one complete project aggregate from its contained database."""

        return self._get_project(project_id, require_supported_schema=True)

    def get_for_deletion(self, project_id: str) -> MigrationProject:
        """Load a deletion target without upgrading its working schema.

        This narrow path exists so a project rejected by the current schema
        baseline can still be removed through the governed project service.
        """

        return self._get_project(project_id, require_supported_schema=False)

    def _get_project(
        self,
        project_id: str,
        *,
        require_supported_schema: bool,
    ) -> MigrationProject:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            if require_supported_schema:
                self._ensure_project_database_schema(connection)
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise ProjectNotFoundError("Project not found")
            columns = [item[0] for item in connection.description]
            data = dict(zip(columns, row, strict=True))
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _project_from_rows(data, source_rows)

    def has_audit_event(self, project_id: str, event_type: str) -> bool:
        """Return whether the project recorded the exact lifecycle event."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
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

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type=event_type,
                    detail=detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_credential_removal_receipt(
        self,
        *,
        receipt_hash: str,
        project_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        """Retain a non-secret vault-removal receipt after project deletion."""

        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO credential_removal_receipt
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    receipt_hash,
                    project_id,
                    role,
                    reason,
                    connection_target_hash,
                    credential_binding_hash,
                    storage_class,
                    removed_at.isoformat(),
                    actor.identity.issuer,
                    actor.identity.subject_id,
                    actor.identity.display_name,
                ],
            )

    def list(self) -> tuple[ProjectSummary, ...]:
        """List registry summaries without scanning contained project databases."""

        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT project_id, name, status, revision, updated_at
                  FROM project_registry
                 ORDER BY updated_at DESC, project_id
                """
            ).fetchall()
        return tuple(
            ProjectSummary(
                project_id=row[0],
                name=row[1],
                status=ProjectStatus(row[2]),
                revision=row[3],
                updated_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        )
    def delete(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """Permanently remove one contained project and its registry row."""

        project_dir = self.project_directory(project_id)
        canonical_project_id = project_dir.name
        database_path = project_dir / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")

        with self._connect(database_path) as connection:
            current = connection.execute(
                "SELECT revision FROM project"
            ).fetchone()
        if current is None:
            raise ProjectNotFoundError("Project not found")
        if int(current[0]) != expected_revision:
            raise ProjectConflictError(
                "The project was modified by another request"
            )

        staged = self.root / f".{canonical_project_id}.deleting-{uuid4()}"
        if staged.parent != self.root or staged.exists():
            raise ProjectError("Could not prepare the project for deletion")

        project_dir.rename(staged)
        registry_deleted = False
        try:
            with self._connect(self.registry_path) as connection:
                registered = connection.execute(
                    """
                    SELECT revision
                      FROM project_registry
                     WHERE project_id = ?
                    """,
                    [canonical_project_id],
                ).fetchone()
                if registered is None:
                    raise ProjectNotFoundError("Project not found")
                if int(registered[0]) != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                connection.execute(
                    "DELETE FROM project_registry WHERE project_id = ?",
                    [canonical_project_id],
                )
                connection.execute(
                    """
                    DELETE FROM project_registry_sync_pending
                     WHERE project_id = ?
                    """,
                    [canonical_project_id],
                )
                registry_deleted = True
            shutil.rmtree(staged)
        except Exception:
            if not registry_deleted and staged.exists() and not project_dir.exists():
                staged.rename(project_dir)
            raise
    def save(
        self,
        project: MigrationProject,
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

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT revision, odoo_connection_mode, odoo_base_url,
                           odoo_database, intended_applications,
                           intended_models, data_manager,
                           functional_owner, retention_days,
                           data_classification
                      FROM project
                    """
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._mark_registry_sync_pending(project.project_id)
                target_changed = event_type == "PROJECT_TARGET_UPDATED" and (
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
                    self._invalidate_canonical_staging(
                        connection,
                        reason="PROJECT_TARGET_CHANGED",
                    )
                elif governance_changed:
                    self._invalidate_quality(
                        connection,
                        reason="PROJECT_GOVERNANCE_CHANGED",
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
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def add_source_file(
        self,
        project: MigrationProject,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Insert one immutable source row without rewriting existing evidence."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                if str(current[1]) == ProjectStatus.CLOSED.value:
                    raise ProjectError("Closed projects cannot be edited")
                if connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM source_selection)"
                ).fetchone()[0]:
                    raise ProjectError(
                        "Source files cannot be changed after table choices are saved"
                    )
                self._mark_registry_sync_pending(project.project_id)
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
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def remove_source_file(
        self,
        project: MigrationProject,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Atomically remove one unfrozen file and its file-scoped evidence."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision, status FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                if str(current[1]) == ProjectStatus.CLOSED.value:
                    raise ProjectError("Closed projects cannot be edited")
                if connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM source_selection)"
                ).fetchone()[0]:
                    raise ProjectError(
                        "Source files cannot be changed after table choices are saved"
                    )
                stored = connection.execute(
                    "SELECT stored_name FROM source_file WHERE file_id = ?",
                    [source_file.file_id],
                ).fetchone()
                if stored is None or str(stored[0]) != source_file.stored_name:
                    raise ProjectError(
                        "The selected source file is no longer in this project"
                    )
                self._mark_registry_sync_pending(project.project_id)
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
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def update_schema_scope(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace Stage C's model allowlist and invalidate its dependents."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._mark_registry_sync_pending(project.project_id)
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
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def synchronize_registration_artifacts(self, project_id: str) -> None:
        """Refresh registry and manifest after another repository updates status."""

        project = self.get(project_id)
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def _update_registry(self, project: MigrationProject) -> None:
        with self._connect(self.registry_path) as connection:
            connection.begin()
            try:
                connection.execute(
                    """
                    UPDATE project_registry
                       SET name = ?, status = ?, revision = ?, updated_at = ?
                     WHERE project_id = ?
                       AND revision <= ?
                    """,
                    [
                        project.name,
                        project.status.value,
                        project.revision,
                        project.updated_at.isoformat(),
                        project.project_id,
                        project.revision,
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO project_registry
                    SELECT ?, ?, ?, ?, ?
                     WHERE NOT EXISTS (
                           SELECT 1
                             FROM project_registry
                            WHERE project_id = ?
                     )
                    """,
                    [
                        project.project_id,
                        project.name,
                        project.status.value,
                        project.revision,
                        project.updated_at.isoformat(),
                        project.project_id,
                    ],
                )
                connection.execute(
                    """
                    DELETE FROM project_registry_sync_pending
                     WHERE project_id = ?
                    """,
                    [project.project_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _mark_registry_sync_pending(self, project_id: str) -> None:
        """Journal a cross-database summary write before project mutation."""

        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO project_registry_sync_pending VALUES (?)
                """,
                [project_id],
            )

    def _recover_pending_registry_sync(self) -> None:
        """Finish only project-summary writes journaled before interruption."""

        with self._connect(self.registry_path) as connection:
            pending = {
                str(row[0])
                for row in connection.execute(
                    "SELECT project_id FROM project_registry_sync_pending"
                ).fetchall()
            }

        for project_id in sorted(pending):
            try:
                project = self.get(project_id)
            except ProjectNotFoundError:
                self._clear_registry_sync_pending(project_id)
                continue
            self._update_registry(project)

    def _clear_registry_sync_pending(self, project_id: str) -> None:
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                DELETE FROM project_registry_sync_pending
                 WHERE project_id = ?
                """,
                [project_id],
            )
    def _insert_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            f"INSERT INTO project VALUES ({', '.join('?' for _ in range(26))})",
            _project_values(project),
        )
    def _update_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            """
            UPDATE project SET
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
    def _write_registration_manifest(self, project: MigrationProject) -> Path:
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
        audit_dir = self.project_directory(project.project_id) / "audit"
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
