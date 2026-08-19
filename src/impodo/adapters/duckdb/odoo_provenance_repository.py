"""Persist protected Odoo capture manifests and encrypted origin sidecars."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os

from ...access import Actor
from ...artifacts import ArtifactStore
from ...domain.odoo_capture import OdooCaptureSelection
from ...domain.odoo_provenance import OdooCaptureManifest
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...domain.serialization import content_hash
from ...domain.source_binding import OdooSourceBinding
from ...domain.source_snapshot import (
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)
from ...projects import ProjectNotFoundError, ProjectStatus, SourceMode
from ...workspace_contracts import SourceSelection
from ...workspace_errors import WorkspaceError
from .database import DuckDbProjectDatabase
from .repository import DuckDbRepository


class OdooProvenanceRepository(DuckDbRepository):
    """Own immutable manifest history and its last-valid current pointer."""

    def __init__(
        self,
        database: DuckDbProjectDatabase,
        artifacts: ArtifactStore,
        *,
        history_quota_bytes: int | None = None,
    ) -> None:
        super().__init__(database)
        self._artifacts = artifacts
        self._history_quota_bytes = (
            history_quota_bytes
            if history_quota_bytes is not None
            else CURRENT_ODOO_SOURCE_POLICY.max_project_history_bytes
        )
        if self._history_quota_bytes < 1:
            raise ValueError("Odoo history quota must be positive")

    def publish_complete_capture(
        self,
        project_id: str,
        manifest: OdooCaptureManifest,
        encrypted_candidate: bytes,
        source_selection: SourceSelection,
        source_snapshot: SourceSnapshot,
        *,
        actor: Actor,
    ) -> None:
        """Promote values, origins, and all current pointers as one publication."""

        _validate_complete_capture(
            project_id,
            manifest,
            source_selection,
            source_snapshot,
        )
        if len(encrypted_candidate) != manifest.provenance_size_bytes:
            raise WorkspaceError("Odoo provenance candidate size is inconsistent")
        # This is the required exact-byte verification at the repository trust
        # boundary. It is one artifact-level pass, never a row-level digest.
        candidate_hash = "sha256:" + sha256(encrypted_candidate).hexdigest()
        if candidate_hash != manifest.provenance_sha256:
            raise WorkspaceError("Odoo provenance candidate hash is inconsistent")
        if manifest.retention_until <= datetime.now(timezone.utc):
            raise WorkspaceError("Odoo provenance candidate is already expired")
        if (
            self._artifacts.source_snapshot_size(
                project_id,
                source_snapshot.parquet_storage_key,
            )
            != manifest.data_size_bytes
        ):
            raise WorkspaceError("Odoo values artifact size is inconsistent")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")

        candidate_path = self._candidate_path(project_id, manifest.manifest_id)
        final_path = self._artifact_path(project_id, manifest.provenance_storage_key)
        if candidate_path.exists() or final_path.exists():
            raise WorkspaceError("Odoo provenance artifact already exists")
        self._write_candidate(candidate_path, encrypted_candidate)
        published = False
        try:
            with self._connect(database_path) as connection:
                self._ensure_project_database_schema(connection)
                project = connection.execute(
                    "SELECT source_mode, status, revision FROM project"
                ).fetchone()
                if project is None:
                    raise ProjectNotFoundError("Project not found")
                if (
                    str(project[0]) != SourceMode.ODOO.value
                    or str(project[1]) != ProjectStatus.REGISTERED.value
                ):
                    raise WorkspaceError(
                        "Only registered Odoo-source projects can publish a capture"
                    )
                selection_row = connection.execute(
                    """
                    SELECT revision.selection_json
                      FROM odoo_capture_selection_current AS current
                      JOIN odoo_capture_selection_revision AS revision
                        ON revision.selection_id = current.selection_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                if selection_row is None:
                    raise WorkspaceError("Current Odoo capture selection is missing")
                selection = OdooCaptureSelection.from_json(str(selection_row[0]))
                if not manifest.binds_selection(selection):
                    raise WorkspaceError(
                        "Odoo capture manifest does not bind the current selection"
                    )
                current_source_row = connection.execute(
                    "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
                ).fetchone()
                current_source = (
                    SourceSelection.from_json(str(current_source_row[0]))
                    if current_source_row is not None
                    else None
                )
                expected_source_version = (
                    current_source.version + 1 if current_source is not None else 1
                )
                if source_selection.version != expected_source_version:
                    raise WorkspaceError(
                        "Source selection was modified by another publication"
                    )
                retained_provenance = connection.execute(
                    """
                    SELECT COALESCE(SUM(provenance_size_bytes), 0)
                      FROM odoo_capture_manifest_revision
                    """
                ).fetchone()
                retained_data_rows = connection.execute(
                    """
                    SELECT data_storage_key, MIN(data_size_bytes), MAX(data_size_bytes)
                      FROM odoo_capture_manifest_revision
                     GROUP BY data_storage_key
                    """
                ).fetchall()
                if any(int(row[1]) != int(row[2]) for row in retained_data_rows):
                    raise WorkspaceError(
                        "Stored Odoo values artifact accounting is inconsistent"
                    )
                retained_data = {str(row[0]): int(row[1]) for row in retained_data_rows}
                existing_data_size = retained_data.get(manifest.data_storage_key)
                if (
                    existing_data_size is not None
                    and existing_data_size != manifest.data_size_bytes
                ):
                    raise WorkspaceError(
                        "Odoo values artifact accounting is inconsistent"
                    )
                retained_bytes = (
                    int(retained_provenance[0]) if retained_provenance else 0
                ) + sum(retained_data.values())
                candidate_bytes = manifest.provenance_size_bytes + (
                    0 if existing_data_size is not None else manifest.data_size_bytes
                )
                if retained_bytes + candidate_bytes > self._history_quota_bytes:
                    raise WorkspaceError("Odoo capture history quota would be exceeded")
                final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                candidate_path.replace(final_path)
                published = True
                connection.begin()
                try:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO source_snapshot_manifest
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            source_snapshot.content_hash,
                            source_snapshot.dataset_id,
                            source_snapshot.logical_hash,
                            source_snapshot.parquet_sha256,
                            source_snapshot.parquet_storage_key,
                            source_snapshot.created_at.isoformat(),
                            source_snapshot.to_json(),
                        ],
                    )
                    registered_snapshot = connection.execute(
                        """
                        SELECT dataset_id, logical_hash, parquet_sha256,
                               parquet_storage_key
                          FROM source_snapshot_manifest
                         WHERE content_hash = ?
                        """,
                        [source_snapshot.content_hash],
                    ).fetchone()
                    if registered_snapshot != (
                        source_snapshot.dataset_id,
                        source_snapshot.logical_hash,
                        source_snapshot.parquet_sha256,
                        source_snapshot.parquet_storage_key,
                    ):
                        raise WorkspaceError(
                            "Stored source snapshot manifest is inconsistent"
                        )
                    connection.execute(
                        "INSERT OR REPLACE INTO source_selection VALUES (1, ?)",
                        [source_selection.to_json()],
                    )
                    connection.execute("DELETE FROM source_snapshot_current")
                    connection.execute(
                        "INSERT INTO source_snapshot_current VALUES (?, ?)",
                        [source_snapshot.dataset_id, source_snapshot.content_hash],
                    )
                    connection.execute(
                        """
                        INSERT INTO odoo_capture_manifest_revision
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            manifest.manifest_id,
                            manifest.content_hash,
                            manifest.selection_hash,
                            manifest.dataset_id,
                            manifest.row_count,
                            manifest.data_storage_key,
                            manifest.data_size_bytes,
                            manifest.provenance_size_bytes,
                            manifest.provenance_storage_key,
                            manifest.retention_until.isoformat(),
                            manifest.capture_finished_at.isoformat(),
                            manifest.to_json(),
                        ],
                    )
                    registered = connection.execute(
                        """
                        SELECT content_hash, selection_hash, dataset_id, row_count,
                               data_storage_key, data_size_bytes,
                               provenance_size_bytes,
                               provenance_storage_key
                          FROM odoo_capture_manifest_revision
                         WHERE manifest_id = ?
                        """,
                        [manifest.manifest_id],
                    ).fetchone()
                    if registered != (
                        manifest.content_hash,
                        manifest.selection_hash,
                        manifest.dataset_id,
                        manifest.row_count,
                        manifest.data_storage_key,
                        manifest.data_size_bytes,
                        manifest.provenance_size_bytes,
                        manifest.provenance_storage_key,
                    ):
                        raise WorkspaceError(
                            "Stored Odoo capture manifest is inconsistent"
                        )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO odoo_capture_manifest_current
                        VALUES (1, ?)
                        """,
                        [manifest.manifest_id],
                    )
                    connection.execute("DELETE FROM derived_entity_plan_current")
                    connection.execute("DELETE FROM mapping_current")
                    self._invalidate_canonical_staging(
                        connection,
                        reason="ODOO_CAPTURE_PUBLISHED",
                    )
                    self._insert_workspace_audit(
                        connection,
                        revision=int(project[2]),
                        event_type="ODOO_SOURCE_CAPTURE_PUBLISHED",
                        detail=(
                            f"manifest {manifest.content_hash}; "
                            f"{manifest.row_count} row(s); "
                            f"{manifest.data_size_bytes} value byte(s); "
                            f"{manifest.provenance_size_bytes} protected byte(s)"
                        ),
                        actor=actor,
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception:
            if published:
                final_path.unlink(missing_ok=True)
            candidate_path.unlink(missing_ok=True)
            raise

    def get_current(self, project_id: str) -> OdooCaptureManifest | None:
        """Restore and verify the one current protected manifest contract."""

        value = self._read_singleton_json(
            project_id,
            """
            SELECT revision.manifest_json
              FROM odoo_capture_manifest_current AS current
              JOIN odoo_capture_manifest_revision AS revision
                ON revision.manifest_id = current.manifest_id
             WHERE current.singleton_id = 1
            """,
        )
        return OdooCaptureManifest.from_json(value) if value else None

    def history(self, project_id: str) -> tuple[OdooCaptureManifest, ...]:
        """Restore immutable capture manifests without opening protected files."""

        return tuple(
            OdooCaptureManifest.from_json(value)
            for value in self._read_json_rows(
                project_id,
                """
                SELECT manifest_json
                  FROM odoo_capture_manifest_revision
                 ORDER BY captured_at, manifest_id
                """,
            )
        )

    def source_snapshot_storage_keys(self, project_id: str) -> frozenset[str]:
        """Return the immutable value artifacts registered by current code."""

        return frozenset(
            self._read_json_rows(
                project_id,
                """
                SELECT parquet_storage_key
                  FROM source_snapshot_manifest
                 ORDER BY parquet_storage_key
                """,
            )
        )

    def recover_incomplete_publications(self, project_id: str) -> int:
        """Remove pending and unreferenced artifacts without moving pointers."""

        referenced_provenance = {
            manifest.provenance_storage_key for manifest in self.history(project_id)
        }
        removed = 0
        root = self._protected_root(project_id)
        candidates = root / "candidates"
        if candidates.is_symlink():
            raise WorkspaceError("Protected Odoo candidate directory is unsafe")
        if candidates.is_dir():
            for candidate in tuple(candidates.iterdir()):
                if candidate.is_dir() and not candidate.is_symlink():
                    raise WorkspaceError("Protected Odoo candidate path is invalid")
                candidate.unlink(missing_ok=True)
                removed += 1
        captures = root / "captures"
        if captures.is_symlink():
            raise WorkspaceError("Protected Odoo capture directory is unsafe")
        if captures.is_dir():
            for candidate in tuple(captures.iterdir()):
                relative = f"captures/{candidate.name}"
                if candidate.is_symlink() or relative not in referenced_provenance:
                    candidate.unlink(missing_ok=True)
                    removed += 1
        removed += self._artifacts.cleanup_source_snapshots(
            project_id,
            self.source_snapshot_storage_keys(project_id),
        )
        return removed

    def read_encrypted(self, project_id: str, manifest: OdooCaptureManifest) -> bytes:
        """Read one contained, bounded protected sidecar for service verification."""

        if manifest.project_id != project_id:
            raise WorkspaceError("Odoo capture manifest belongs to another project")
        path = self._artifact_path(project_id, manifest.provenance_storage_key)
        if path.is_symlink() or not path.is_file():
            raise WorkspaceError("Stored Odoo provenance artifact is missing")
        if path.stat().st_size != manifest.provenance_size_bytes:
            raise WorkspaceError("Stored Odoo provenance artifact size changed")
        with path.open("rb") as stream:
            value = stream.read(manifest.provenance_size_bytes + 1)
        if len(value) != manifest.provenance_size_bytes:
            raise WorkspaceError("Stored Odoo provenance artifact is oversized")
        return value

    def invalidate_current(
        self,
        project_id: str,
        *,
        reason: str,
        actor: Actor,
    ) -> bool:
        """Retire all current capture roots while preserving immutable history."""

        if not reason.strip() or len(reason) > 200:
            raise WorkspaceError("Odoo provenance invalidation reason is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            exists = connection.execute(
                """
                SELECT manifest_id
                  FROM odoo_capture_manifest_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if exists is None:
                return False
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute("DELETE FROM odoo_capture_manifest_current")
                connection.execute("DELETE FROM source_selection")
                connection.execute("DELETE FROM source_snapshot_current")
                connection.execute("DELETE FROM derived_entity_plan_current")
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="ODOO_CAPTURE_INVALIDATED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="ODOO_CAPTURE_PROVENANCE_INVALIDATED",
                    detail=reason,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def purge_expired_history(
        self,
        project_id: str,
        *,
        now: datetime,
        actor: Actor,
    ) -> int:
        """Delete expired non-current artifacts and their manifest revisions."""

        if now.tzinfo is None:
            raise WorkspaceError("Odoo retention time must be timezone-aware")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            rows = connection.execute(
                """
                SELECT manifest_id, provenance_storage_key, data_storage_key
                  FROM odoo_capture_manifest_revision
                 WHERE retention_until <= ?
                   AND manifest_id NOT IN (
                       SELECT manifest_id FROM odoo_capture_manifest_current
                   )
                 ORDER BY manifest_id
                """,
                [now.astimezone(timezone.utc).isoformat()],
            ).fetchall()
            if not rows:
                return 0
            expired_data_keys = {str(row[2]) for row in rows}
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.executemany(
                    "DELETE FROM odoo_capture_manifest_revision WHERE manifest_id = ?",
                    [[str(row[0])] for row in rows],
                )
                retained_data_keys = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT data_storage_key
                          FROM odoo_capture_manifest_revision
                        """
                    ).fetchall()
                }
                removable_data_keys = expired_data_keys - retained_data_keys
                if removable_data_keys:
                    connection.executemany(
                        """
                        DELETE FROM source_snapshot_manifest
                         WHERE parquet_storage_key = ?
                           AND content_hash NOT IN (
                               SELECT content_hash FROM source_snapshot_current
                           )
                        """,
                        [[storage_key] for storage_key in sorted(removable_data_keys)],
                    )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="ODOO_CAPTURE_PROVENANCE_RETAINED_HISTORY_PURGED",
                    detail=f"{len(rows)} expired manifest(s)",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        for _, storage_key, _ in rows:
            self._artifact_path(project_id, str(storage_key)).unlink(missing_ok=True)
        self._artifacts.cleanup_source_snapshots(
            project_id,
            self.source_snapshot_storage_keys(project_id),
        )
        return len(rows)

    def _write_candidate(self, path: Path, value: bytes) -> None:
        if len(value) > CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes:
            raise WorkspaceError("Odoo provenance candidate exceeds snapshot limit")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _candidate_path(self, project_id: str, manifest_id: str) -> Path:
        root = self._protected_root(project_id)
        candidates = root / "candidates"
        if candidates.is_symlink():
            raise WorkspaceError("Protected Odoo candidate directory is unsafe")
        candidates.mkdir(mode=0o700, exist_ok=True)
        if candidates.resolve().parent != root.resolve():
            raise WorkspaceError(
                "Protected Odoo candidate directory escapes the project"
            )
        return candidates / f"{manifest_id}.pending"

    def _artifact_path(self, project_id: str, storage_key: str) -> Path:
        root = self._protected_root(project_id)
        captures = root / "captures"
        if captures.is_symlink():
            raise WorkspaceError("Protected Odoo capture directory is unsafe")
        captures.mkdir(mode=0o700, exist_ok=True)
        candidate = root / storage_key
        resolved = candidate.resolve()
        if (
            captures.resolve().parent != root.resolve()
            or resolved.parent != captures.resolve()
        ):
            raise WorkspaceError("Odoo provenance storage key escapes the project")
        return candidate

    def _protected_root(self, project_id: str) -> Path:
        root = self.project_directory(project_id) / "protected"
        if root.is_symlink():
            raise WorkspaceError("Protected Odoo evidence directory is unsafe")
        root.mkdir(mode=0o700, exist_ok=True)
        return root


def _validate_complete_capture(
    project_id: str,
    manifest: OdooCaptureManifest,
    selection: SourceSelection,
    snapshot: SourceSnapshot,
) -> None:
    if (
        manifest.project_id != project_id
        or selection.project_id != project_id
        or len(selection.datasets) != 1
    ):
        raise WorkspaceError("Odoo capture publication belongs to another project")
    expected_selection_hash = content_hash(
        {
            "project_id": project_id,
            "version": selection.version,
            "datasets": [item.to_dict() for item in selection.datasets],
        }
    )
    dataset = selection.datasets[0]
    expected_source = OdooSourceBinding(
        capture_selection_hash=manifest.selection_hash,
        model=manifest.model,
        policy_hash=manifest.policy_hash,
        connection_target_hash=manifest.connection_target_hash,
        schema_scope_hash=manifest.schema_scope_hash,
        read_principal_hash=manifest.read_principal_hash,
        read_permission_hash=manifest.read_permission_hash,
        context_hash=manifest.context_hash,
    )
    expected_schema = SourceSnapshotSchema.create(
        SourceSnapshotColumn.create(
            ordinal=column.ordinal,
            stable_key=column.stable_key,
            source_name=column.source_name,
            candidate_type=column.candidate_type,
        )
        for column in dataset.columns
    )
    if (
        selection.content_hash != expected_selection_hash
        or dataset.source != expected_source
        or dataset.dataset_id != manifest.dataset_id
        or dataset.name != manifest.dataset_name
        or dataset.row_count != manifest.row_count
        or tuple(item.source_name for item in dataset.columns) != manifest.field_names
        or tuple(item.stable_key for item in dataset.columns)
        != manifest.column_stable_keys
        or snapshot.project_id != project_id
        or snapshot.dataset_id != dataset.dataset_id
        or snapshot.dataset_name != dataset.name
        or snapshot.source != dataset.source
        or snapshot.physical_selection_hash != selection.content_hash
        or snapshot.schema != expected_schema
        or snapshot.row_count != dataset.row_count
        or snapshot.data_logical_hash != manifest.data_logical_hash
        or snapshot.parquet_sha256 != manifest.data_sha256
        or snapshot.parquet_storage_key != manifest.data_storage_key
    ):
        raise WorkspaceError(
            "Odoo values, provenance, and source snapshot bindings are inconsistent"
        )
