"""Persist protected Odoo capture manifests and encrypted origin sidecars."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os

from ...access import Actor
from ...domain.odoo_capture import OdooCaptureSelection
from ...domain.odoo_provenance import OdooCaptureManifest
from ...domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...projects import ProjectNotFoundError, SourceMode
from ...workspace_errors import WorkspaceError
from .database import DuckDbDatabase
from .repository import DuckDbRepository


class OdooProvenanceRepository(DuckDbRepository):
    """Own immutable manifest history and its last-valid current pointer."""

    def __init__(
        self,
        database: DuckDbDatabase,
        *,
        history_quota_bytes: int | None = None,
    ) -> None:
        super().__init__(database)
        self._history_quota_bytes = (
            history_quota_bytes
            if history_quota_bytes is not None
            else CURRENT_ODOO_SOURCE_POLICY.max_project_history_bytes
        )
        if self._history_quota_bytes < 1:
            raise ValueError("Odoo history quota must be positive")

    def publish(
        self,
        project_id: str,
        manifest: OdooCaptureManifest,
        encrypted_candidate: bytes,
        *,
        actor: Actor,
    ) -> None:
        """Verify one candidate, publish it, then advance the pointer atomically."""

        if manifest.project_id != project_id:
            raise WorkspaceError("Odoo capture manifest belongs to another project")
        if len(encrypted_candidate) != manifest.provenance_size_bytes:
            raise WorkspaceError("Odoo provenance candidate size is inconsistent")
        # This is the required exact-byte verification at the repository trust
        # boundary. It is one artifact-level pass, never a row-level digest.
        candidate_hash = "sha256:" + sha256(encrypted_candidate).hexdigest()
        if candidate_hash != manifest.provenance_sha256:
            raise WorkspaceError("Odoo provenance candidate hash is inconsistent")
        if manifest.retention_until <= datetime.now(timezone.utc):
            raise WorkspaceError("Odoo provenance candidate is already expired")
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
                    "SELECT source_mode, revision FROM project"
                ).fetchone()
                if project is None:
                    raise ProjectNotFoundError("Project not found")
                if str(project[0]) != SourceMode.ODOO.value:
                    raise WorkspaceError(
                        "Only Odoo-source projects can publish Odoo provenance"
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
                retained = connection.execute(
                    """
                    SELECT COALESCE(SUM(data_size_bytes + provenance_size_bytes), 0)
                      FROM odoo_capture_manifest_revision
                    """
                ).fetchone()
                retained_bytes = int(retained[0]) if retained else 0
                candidate_bytes = (
                    manifest.data_size_bytes + manifest.provenance_size_bytes
                )
                if retained_bytes + candidate_bytes > self._history_quota_bytes:
                    raise WorkspaceError(
                        "Odoo capture history quota would be exceeded"
                    )
                final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                candidate_path.replace(final_path)
                published = True
                connection.begin()
                try:
                    connection.execute(
                        """
                        INSERT INTO odoo_capture_manifest_revision
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            manifest.manifest_id,
                            manifest.content_hash,
                            manifest.selection_hash,
                            manifest.dataset_id,
                            manifest.row_count,
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
                               data_size_bytes, provenance_size_bytes,
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
                    self._insert_workspace_audit(
                        connection,
                        revision=int(project[1]),
                        event_type="ODOO_CAPTURE_PROVENANCE_PUBLISHED",
                        detail=(
                            f"manifest {manifest.content_hash}; "
                            f"{manifest.row_count} row(s); "
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
        """Retire only the current pointer while preserving immutable history."""

        if not reason.strip() or len(reason) > 200:
            raise WorkspaceError("Odoo provenance invalidation reason is invalid")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            exists = connection.execute(
                "SELECT manifest_id FROM odoo_capture_manifest_current WHERE singleton_id = 1"
            ).fetchone()
            if exists is None:
                return False
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute("DELETE FROM odoo_capture_manifest_current")
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
                SELECT manifest_id, provenance_storage_key
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
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.executemany(
                    "DELETE FROM odoo_capture_manifest_revision WHERE manifest_id = ?",
                    [[str(row[0])] for row in rows],
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
        for _, storage_key in rows:
            self._artifact_path(project_id, str(storage_key)).unlink(missing_ok=True)
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
            raise WorkspaceError("Protected Odoo candidate directory escapes the project")
        return candidates / f"{manifest_id}.pending"

    def _artifact_path(self, project_id: str, storage_key: str) -> Path:
        root = self._protected_root(project_id)
        captures = root / "captures"
        if captures.is_symlink():
            raise WorkspaceError("Protected Odoo capture directory is unsafe")
        captures.mkdir(mode=0o700, exist_ok=True)
        candidate = root / storage_key
        resolved = candidate.resolve()
        if captures.resolve().parent != root.resolve() or resolved.parent != captures.resolve():
            raise WorkspaceError("Odoo provenance storage key escapes the project")
        return candidate

    def _protected_root(self, project_id: str) -> Path:
        root = self.project_directory(project_id) / "protected"
        if root.is_symlink():
            raise WorkspaceError("Protected Odoo evidence directory is unsafe")
        root.mkdir(mode=0o700, exist_ok=True)
        return root
