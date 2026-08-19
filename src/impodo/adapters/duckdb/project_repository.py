"""Persist Stage A project state, registry rows, files, and audit evidence.

Layer: DuckDB/filesystem adapter. ``ProjectRepository`` implements the port
used by ``ProjectService``. It owns optimistic transactions, the per-project
directory/database boundary, lightweight registry synchronization, registration
manifests, and downstream invalidation caused by project-level changes.

See ``docs/architecture/python-code-map.md`` and ``tests/test_projects.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import duckdb

from ...access import Actor
from ...projects import (
    MigrationProject,
    ProjectConflictError,
    ProjectCreationReplayError,
    ProjectError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectSummary,
    SourceFile,
)
from ...recipes import require_hash, require_uuid
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
        self._recover_unlinked_workspaces()

    def create(
        self,
        project: MigrationProject,
        *,
        recipe_id: str,
        data_version_id: str,
        creation_request_id: str | None = None,
        creation_request_hash: str | None = None,
        actor: Actor,
    ) -> None:
        """Create one Recipe and its first authoring workspace."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        if (creation_request_id is None) != (creation_request_hash is None):
            raise ProjectError("Recipe creation request identity is incomplete")
        if creation_request_id is not None:
            creation_request_id = require_uuid(
                creation_request_id,
                "creation_request_id",
            )
            creation_request_hash = require_hash(
                creation_request_hash or "",
                "creation_request_hash",
            )
            replay = self._creation_request_replay(creation_request_id)
            if replay is not None:
                raise ProjectCreationReplayError(*replay)
        if len({project.project_id, recipe_id, data_version_id}) != 3:
            raise ProjectError("Recipe workspace identities must be distinct")
        self._create_workspace(
            project,
            actor=actor,
            recipe_id=recipe_id,
            data_version_id=data_version_id,
            creation_request_id=creation_request_id,
            creation_request_hash=creation_request_hash,
        )

    def create_unlinked(self, project: MigrationProject, *, actor: Actor) -> None:
        """Create a contained workspace before DataVersion adoption."""

        self._create_workspace(project, actor=actor)

    def _create_workspace(
        self,
        project: MigrationProject,
        *,
        actor: Actor,
        recipe_id: str | None = None,
        data_version_id: str | None = None,
        creation_request_id: str | None = None,
        creation_request_hash: str | None = None,
    ) -> None:
        self._mark_registry_sync_pending(
            project.project_id,
            recipe_id=recipe_id,
            data_version_id=data_version_id,
            creation_request_id=creation_request_id,
            creation_request_hash=creation_request_hash,
        )
        project_dir = self.project_directory(project.project_id)
        try:
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
            self._update_registry(
                project,
                recipe_id=recipe_id,
                data_version_id=data_version_id,
                creation_request_id=creation_request_id,
                creation_request_hash=creation_request_hash,
            )
        except Exception as error:
            if project_dir.is_dir():
                shutil.rmtree(project_dir)
            self._clear_registry_sync_pending(project.project_id)
            if creation_request_id is not None:
                replay = self._creation_request_replay(creation_request_id)
                if replay is not None:
                    raise ProjectCreationReplayError(*replay) from error
            raise

    def discard_unlinked(self, project_id: str) -> None:
        """Remove only a workspace that has not joined any Recipe lineage."""

        project_dir = self.project_directory(project_id)
        with self._connect(self.registry_path) as connection:
            linked = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM data_version "
                "WHERE workspace_project_id = ?)",
                [project_id],
            ).fetchone()
            if linked and bool(linked[0]):
                return
            connection.execute(
                "DELETE FROM project_registry WHERE project_id = ?",
                [project_id],
            )
            connection.execute(
                "DELETE FROM project_registry_sync_pending WHERE project_id = ?",
                [project_id],
            )
        if project_dir.is_dir():
            shutil.rmtree(project_dir)

    def get(self, project_id: str) -> MigrationProject:
        """Load one complete project aggregate from its contained database."""

        return self._get_project(project_id)

    def _get_project(self, project_id: str) -> MigrationProject:
        resolution = self._recipe_workspace_resolution(project_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            self._ensure_workspace_linkage(connection, resolution)
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

    def assert_workspace_mutable(self, project_id: str) -> None:
        """Reject writes to sealed DataVersion workspaces."""

        resolution = self._recipe_workspace_resolution(project_id)
        if resolution[3] != "ACTIVE":
            raise ProjectError("This historical data version is read-only")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            self._ensure_workspace_linkage(connection, resolution)
            sealed = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM recipe_workspace_seal)"
            ).fetchone()
        if sealed and bool(sealed[0]):
            raise ProjectError("This historical data version is read-only")

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
                 WHERE EXISTS (
                       SELECT 1 FROM data_version
                        WHERE workspace_project_id = project_registry.project_id
                 )
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

    def _recipe_workspace_resolution(
        self,
        project_id: str,
    ) -> tuple[str, str, int, str]:
        """Resolve a project route through explicit Recipe/DataVersion identity."""

        canonical_project_id = self.project_directory(project_id).name
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT d.recipe_id, d.data_version_id, d.version_number,
                       d.state
                  FROM data_version d
                  JOIN recipe r ON r.recipe_id = d.recipe_id
                 WHERE d.workspace_project_id = ?
                """,
                [canonical_project_id],
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError("Project is not linked to a Recipe")
        return str(row[0]), str(row[1]), int(row[2]), str(row[3])

    @staticmethod
    def _ensure_workspace_linkage(
        connection: duckdb.DuckDBPyConnection,
        resolution: tuple[str, str, int, str],
    ) -> None:
        recipe_id, data_version_id, version_number, data_version_state = resolution
        current = connection.execute(
            """
            SELECT recipe_id, data_version_id, data_version_number
              FROM recipe_workspace_linkage
             WHERE singleton_id = 1
            """
        ).fetchone()
        expected = (recipe_id, data_version_id, version_number)
        if current is None:
            connection.execute(
                """
                INSERT INTO recipe_workspace_linkage
                VALUES (1, ?, ?, ?, ?)
                """,
                [
                    recipe_id,
                    data_version_id,
                    version_number,
                    datetime.now(timezone.utc).isoformat(),
                ],
            )
        elif (str(current[0]), str(current[1]), int(current[2])) != expected:
            raise ProjectError("Workspace Recipe/DataVersion linkage is inconsistent")
        if data_version_state == "SEALED":
            connection.execute(
                """
                INSERT OR IGNORE INTO recipe_workspace_seal
                VALUES (1, ?, 'DATA_VERSION_SEALED')
                """,
                [datetime.now(timezone.utc).isoformat()],
            )
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

    def _update_registry(
        self,
        project: MigrationProject,
        *,
        recipe_id: str | None = None,
        data_version_id: str | None = None,
        creation_request_id: str | None = None,
        creation_request_hash: str | None = None,
    ) -> None:
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
                if recipe_id is not None or data_version_id is not None:
                    if recipe_id is None or data_version_id is None:
                        raise ProjectError("Recipe workspace identity is incomplete")
                    self._insert_initial_recipe_workspace(
                        connection,
                        project=project,
                        recipe_id=recipe_id,
                        data_version_id=data_version_id,
                        creation_request_id=creation_request_id,
                        creation_request_hash=creation_request_hash,
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

    def _mark_registry_sync_pending(
        self,
        project_id: str,
        *,
        recipe_id: str | None = None,
        data_version_id: str | None = None,
        creation_request_id: str | None = None,
        creation_request_hash: str | None = None,
    ) -> None:
        """Journal a cross-database summary write before project mutation."""

        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO project_registry_sync_pending (
                    project_id, recipe_id, data_version_id,
                    creation_request_id, creation_request_hash
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (project_id) DO UPDATE SET
                    recipe_id = coalesce(
                        excluded.recipe_id,
                        project_registry_sync_pending.recipe_id
                    ),
                    data_version_id = coalesce(
                        excluded.data_version_id,
                        project_registry_sync_pending.data_version_id
                    ),
                    creation_request_id = coalesce(
                        excluded.creation_request_id,
                        project_registry_sync_pending.creation_request_id
                    ),
                    creation_request_hash = coalesce(
                        excluded.creation_request_hash,
                        project_registry_sync_pending.creation_request_hash
                    )
                """,
                [
                    project_id,
                    recipe_id,
                    data_version_id,
                    creation_request_id,
                    creation_request_hash,
                ],
            )

    def _recover_pending_registry_sync(self) -> None:
        """Finish only project-summary writes journaled before interruption."""

        with self._connect(self.registry_path) as connection:
            pending = tuple(
                connection.execute(
                    "SELECT project_id, recipe_id, data_version_id, "
                    "creation_request_id, creation_request_hash "
                    "FROM project_registry_sync_pending ORDER BY project_id"
                ).fetchall()
            )

        for (
            project_value,
            recipe_value,
            data_version_value,
            creation_request_value,
            creation_request_hash_value,
        ) in pending:
            project_id = str(project_value)
            creation_request_id = (
                str(creation_request_value) if creation_request_value else None
            )
            if creation_request_id is not None:
                replay = self._creation_request_replay(creation_request_id)
                if replay is not None and replay[0] != project_id:
                    self.discard_unlinked(project_id)
                    continue
            try:
                project = self._get_project_unresolved(project_id)
            except ProjectNotFoundError:
                self._clear_registry_sync_pending(project_id)
                continue
            self._update_registry(
                project,
                recipe_id=(str(recipe_value) if recipe_value else None),
                data_version_id=(
                    str(data_version_value) if data_version_value else None
                ),
                creation_request_id=creation_request_id,
                creation_request_hash=(
                    str(creation_request_hash_value)
                    if creation_request_hash_value
                    else None
                ),
            )

    def _recover_unlinked_workspaces(self) -> None:
        """Discard provisional workspaces with no recoverable creation intent."""

        with self._connect(self.registry_path) as connection:
            provisional = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT p.project_id
                      FROM project_registry p
                     WHERE NOT EXISTS (
                           SELECT 1 FROM data_version d
                            WHERE d.workspace_project_id = p.project_id
                     )
                     ORDER BY p.project_id
                    """
                ).fetchall()
            )
            intent_details = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT detail_json FROM recipe_intent
                     WHERE kind = 'DATA_VERSION_CREATION'
                       AND state NOT IN ('COMPLETE', 'ABANDONED')
                    """
                ).fetchall()
            )
        retained = {
            str(detail.get("workspace_project_id"))
            for encoded in intent_details
            if isinstance((detail := json.loads(encoded)), dict)
            and detail.get("workspace_project_id")
        }
        for project_id in provisional:
            if project_id in retained:
                continue
            self.discard_unlinked(project_id)

    def _get_project_unresolved(self, project_id: str) -> MigrationProject:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise ProjectNotFoundError("Project not found")
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

    @staticmethod
    def _insert_initial_recipe_workspace(
        connection: duckdb.DuckDBPyConnection,
        *,
        project: MigrationProject,
        recipe_id: str,
        data_version_id: str,
        creation_request_id: str | None,
        creation_request_hash: str | None,
    ) -> None:
        existing = connection.execute(
            """
            SELECT d.recipe_id, d.data_version_id
              FROM data_version d
             WHERE d.workspace_project_id = ?
            """,
            [project.project_id],
        ).fetchone()
        if existing is not None:
            if (str(existing[0]), str(existing[1])) != (
                recipe_id,
                data_version_id,
            ):
                raise ProjectError("Workspace is already linked to another Recipe")
            return
        collision = connection.execute(
            """
            SELECT
                EXISTS (SELECT 1 FROM recipe WHERE recipe_id IN (?, ?)),
                EXISTS (SELECT 1 FROM data_version WHERE data_version_id IN (?, ?)),
                EXISTS (SELECT 1 FROM project_registry WHERE project_id IN (?, ?))
            """,
            [
                recipe_id,
                data_version_id,
                recipe_id,
                data_version_id,
                recipe_id,
                data_version_id,
            ],
        ).fetchone()
        if collision and any(bool(value) for value in collision):
            raise ProjectError("Recipe workspace identity is already in use")
        now = project.updated_at.isoformat()
        connection.execute(
            """
            INSERT INTO recipe (
                recipe_id, display_name, business_purpose,
                data_classification, retention_days, current_recipe_revision,
                current_data_version_id, cutover_candidate_id,
                optimistic_revision, created_at, updated_at,
                creation_request_id, creation_request_hash
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, 1, ?, ?, ?, ?)
            """,
            [
                recipe_id,
                project.name,
                project.description or project.name,
                project.data_classification.value,
                project.retention_days,
                data_version_id,
                project.created_at.isoformat(),
                now,
                creation_request_id,
                creation_request_hash,
            ],
        )
        connection.execute(
            """
            INSERT INTO data_version (
                data_version_id, recipe_id, version_number,
                workspace_project_id, parent_data_version_id, purpose, state,
                pinned_recipe_revision, label, export_as_of_date,
                parameter_values_hash, created_at, sealed_at
            ) VALUES (?, ?, 1, ?, NULL, 'AUTHORING', 'ACTIVE', NULL, ?, ?, NULL,
                      ?, NULL)
            """,
            [
                data_version_id,
                recipe_id,
                project.project_id,
                f"{project.name} data version 1",
                project.export_date.isoformat() if project.export_date else None,
                project.created_at.isoformat(),
            ],
        )

    def _creation_request_replay(
        self,
        creation_request_id: str,
    ) -> tuple[str, str] | None:
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT data.workspace_project_id, recipe.creation_request_hash
                  FROM recipe
                  JOIN data_version data
                    ON data.recipe_id = recipe.recipe_id
                   AND data.version_number = 1
                 WHERE recipe.creation_request_id = ?
                """,
                [creation_request_id],
            ).fetchone()
        if row is None:
            return None
        if not row[1]:
            raise ProjectError("Stored Recipe creation request is invalid")
        return str(row[0]), str(row[1])

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
