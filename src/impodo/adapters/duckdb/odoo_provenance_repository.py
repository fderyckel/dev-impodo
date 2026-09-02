"""Persist protected Odoo capture manifests and encrypted origin sidecars."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os
from typing import Callable

from impodo.domain.shared.access import Actor
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
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
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError, WorkspaceStatus, SourceMode
from impodo.domain.workspace.contracts import (
    SourceSelection,
    WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
)
from impodo.domain.workspace.errors import WorkspaceError
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


class OdooProvenanceRepository(DuckDbRepository):
    """Own immutable manifest history and its last-valid current pointer."""

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        artifacts: DataVersionSourceArtifactStore,
        *,
        history_quota_bytes: int | None = None,
        protected_root: Callable[[str], Path],
    ) -> None:
        super().__init__(database)
        self._artifacts = artifacts
        self._history_quota_bytes = (
            history_quota_bytes
            if history_quota_bytes is not None
            else CURRENT_ODOO_SOURCE_POLICY.max_project_history_bytes
        )
        self._protected_root_resolver = protected_root
        if self._history_quota_bytes < 1:
            raise ValueError("Odoo history quota must be positive")

    def publish_complete_capture(
        self,
        workspace_id: str,
        manifest: OdooCaptureManifest,
        encrypted_candidate: bytes,
        source_selection: SourceSelection,
        source_snapshot: SourceSnapshot,
        *,
        actor: Actor,
    ) -> None:
        """Backward-compatible one-dataset entrypoint."""

        self.publish_complete_captures(
            workspace_id,
            ((manifest, encrypted_candidate),),
            source_selection,
            (source_snapshot,),
            actor=actor,
        )

    def publish_complete_captures(
        self,
        workspace_id: str,
        protected_candidates: tuple[tuple[OdooCaptureManifest, bytes], ...],
        source_selection: SourceSelection,
        source_snapshots: tuple[SourceSnapshot, ...],
        *,
        actor: Actor,
    ) -> None:
        """Promote a complete multi-model values/provenance set atomically."""

        self._assert_workspace_mutable(workspace_id)
        manifests = tuple(item[0] for item in protected_candidates)
        encrypted = {item[0].manifest_id: item[1] for item in protected_candidates}
        _validate_complete_captures(
            workspace_id,
            manifests,
            source_selection,
            source_snapshots,
        )
        if len(encrypted) != len(manifests):
            raise WorkspaceError("Odoo provenance candidates are duplicated")
        now = datetime.now(timezone.utc)
        for manifest in manifests:
            value = encrypted[manifest.manifest_id]
            if len(value) != manifest.provenance_size_bytes:
                raise WorkspaceError(
                    "Odoo provenance candidate size is inconsistent"
                )
            if "sha256:" + sha256(value).hexdigest() != manifest.provenance_sha256:
                raise WorkspaceError(
                    "Odoo provenance candidate hash is inconsistent"
                )
            if manifest.retention_until <= now:
                raise WorkspaceError(
                    "Odoo provenance candidate is already expired"
                )
        snapshot_by_id = {item.dataset_id: item for item in source_snapshots}
        for manifest in manifests:
            snapshot = snapshot_by_id[manifest.dataset_id]
            if (
                self._artifacts.source_snapshot_size(
                    snapshot.data_version_id,
                    snapshot.parquet_storage_key,
                )
                != manifest.data_size_bytes
            ):
                raise WorkspaceError("Odoo values artifact size is inconsistent")

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        paths = tuple(
            (
                manifest,
                self._candidate_path(workspace_id, manifest.manifest_id),
                self._artifact_path(
                    workspace_id,
                    manifest.provenance_storage_key,
                ),
            )
            for manifest in manifests
        )
        if any(candidate.exists() or final.exists() for _, candidate, final in paths):
            raise WorkspaceError("Odoo provenance artifact already exists")
        for manifest, candidate, _ in paths:
            self._write_candidate(candidate, encrypted[manifest.manifest_id])
        published_paths: list[Path] = []
        try:
            with self._connect(database_path) as connection:
                self._ensure_workspace_database_schema(connection)
                workspace_projection = connection.execute(
                    "SELECT source_mode, status, revision "
                    "FROM workspace_projection_cache"
                ).fetchone()
                if workspace_projection is None:
                    raise WorkspaceStateNotFoundError(
                        "Workspace engine state not found"
                    )
                if (
                    str(workspace_projection[0]) != SourceMode.ODOO.value
                    or str(workspace_projection[1])
                    != WorkspaceStatus.REGISTERED.value
                ):
                    raise WorkspaceError(
                        "Only registered Odoo-source workspaces can publish a capture"
                    )
                current_selection_rows = connection.execute(
                    """
                    SELECT revision.selection_json
                      FROM odoo_capture_selection_current AS current_selection
                      JOIN odoo_capture_selection_revision AS revision
                        ON revision.selection_id = current_selection.selection_id
                       AND revision.version = current_selection.version
                     ORDER BY current_selection.model
                    """
                ).fetchall()
                current_selections = tuple(
                    OdooCaptureSelection.from_json(str(row[0]))
                    for row in current_selection_rows
                )
                selection_by_dataset = {
                    item.dataset_id: item for item in current_selections
                }
                if (
                    set(selection_by_dataset)
                    != {item.dataset_id for item in manifests}
                    or any(
                        not manifest.binds_selection(
                            selection_by_dataset[manifest.dataset_id]
                        )
                        for manifest in manifests
                    )
                ):
                    raise WorkspaceError(
                        "Odoo capture manifests do not bind the current selections"
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
                retained_data = {
                    str(row[0]): int(row[1]) for row in retained_data_rows
                }
                candidate_data: dict[str, int] = {}
                for manifest in manifests:
                    existing = retained_data.get(manifest.data_storage_key)
                    if existing is not None and existing != manifest.data_size_bytes:
                        raise WorkspaceError(
                            "Odoo values artifact accounting is inconsistent"
                        )
                    previous = candidate_data.setdefault(
                        manifest.data_storage_key,
                        manifest.data_size_bytes,
                    )
                    if previous != manifest.data_size_bytes:
                        raise WorkspaceError(
                            "Odoo values candidates have inconsistent accounting"
                        )
                retained_bytes = (
                    int(retained_provenance[0]) if retained_provenance else 0
                ) + sum(retained_data.values())
                candidate_bytes = sum(
                    item.provenance_size_bytes for item in manifests
                ) + sum(
                    size
                    for key, size in candidate_data.items()
                    if key not in retained_data
                )
                if retained_bytes + candidate_bytes > self._history_quota_bytes:
                    raise WorkspaceError(
                        "Odoo capture history quota would be exceeded"
                    )
                for _, candidate, final in paths:
                    _mkdir_private(final.parent, parents=True)
                    candidate.replace(final)
                    published_paths.append(final)

                connection.begin()
                try:
                    for snapshot in source_snapshots:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO source_snapshot_manifest
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                snapshot.content_hash,
                                snapshot.dataset_id,
                                snapshot.logical_hash,
                                snapshot.parquet_sha256,
                                snapshot.parquet_storage_key,
                                snapshot.created_at.isoformat(),
                                snapshot.to_json(),
                            ],
                        )
                        registered_snapshot = connection.execute(
                            """
                            SELECT dataset_id, logical_hash, parquet_sha256,
                                   parquet_storage_key
                              FROM source_snapshot_manifest
                             WHERE content_hash = ?
                            """,
                            [snapshot.content_hash],
                        ).fetchone()
                        if registered_snapshot != (
                            snapshot.dataset_id,
                            snapshot.logical_hash,
                            snapshot.parquet_sha256,
                            snapshot.parquet_storage_key,
                        ):
                            raise WorkspaceError(
                                "Stored source snapshot manifest is inconsistent"
                            )
                    connection.execute(
                        "INSERT OR REPLACE INTO source_selection VALUES (1, ?)",
                        [source_selection.to_json()],
                    )
                    connection.execute("DELETE FROM source_snapshot_current")
                    connection.executemany(
                        "INSERT INTO source_snapshot_current VALUES (?, ?)",
                        [
                            [snapshot.dataset_id, snapshot.content_hash]
                            for snapshot in source_snapshots
                        ],
                    )
                    for manifest in manifests:
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
                    connection.execute("DELETE FROM odoo_capture_manifest_current")
                    connection.executemany(
                        "INSERT INTO odoo_capture_manifest_current VALUES (?, ?)",
                        [
                            [manifest.dataset_id, manifest.manifest_id]
                            for manifest in manifests
                        ],
                    )
                    connection.execute("DELETE FROM derived_entity_plan_current")
                    connection.execute("DELETE FROM mapping_current")
                    self._invalidate_canonical_staging(
                        connection,
                        reason="ODOO_CAPTURE_PUBLISHED",
                    )
                    self._insert_workspace_audit(
                        connection,
                        revision=int(workspace_projection[2]),
                        event_type="ODOO_SOURCE_CAPTURE_PUBLISHED",
                        detail=(
                            f"{len(manifests)} dataset(s); "
                            f"{sum(item.row_count for item in manifests)} row(s); "
                            f"{sum(item.data_size_bytes for item in manifests)} "
                            "value byte(s)"
                        ),
                        actor=actor,
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception:
            for final in published_paths:
                final.unlink(missing_ok=True)
            for _, candidate, _ in paths:
                candidate.unlink(missing_ok=True)
            raise

    def _publish_complete_capture_legacy(
        self,
        workspace_id: str,
        manifest: OdooCaptureManifest,
        encrypted_candidate: bytes,
        source_selection: SourceSelection,
        source_snapshot: SourceSnapshot,
        *,
        actor: Actor,
    ) -> None:
        """Promote values, origins, and all current pointers as one publication."""

        self._assert_workspace_mutable(workspace_id)
        _validate_complete_capture(
            workspace_id,
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
                source_snapshot.data_version_id,
                source_snapshot.parquet_storage_key,
            )
            != manifest.data_size_bytes
        ):
            raise WorkspaceError("Odoo values artifact size is inconsistent")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")

        candidate_path = self._candidate_path(workspace_id, manifest.manifest_id)
        final_path = self._artifact_path(workspace_id, manifest.provenance_storage_key)
        if candidate_path.exists() or final_path.exists():
            raise WorkspaceError("Odoo provenance artifact already exists")
        self._write_candidate(candidate_path, encrypted_candidate)
        published = False
        try:
            with self._connect(database_path) as connection:
                self._ensure_workspace_database_schema(connection)
                workspace_projection = connection.execute(
                    "SELECT source_mode, status, revision "
                    "FROM workspace_projection_cache"
                ).fetchone()
                if workspace_projection is None:
                    raise WorkspaceStateNotFoundError("Workspace engine state not found")
                if (
                    str(workspace_projection[0]) != SourceMode.ODOO.value
                    or str(workspace_projection[1]) != WorkspaceStatus.REGISTERED.value
                ):
                    raise WorkspaceError(
                        "Only registered Odoo-source workspaces can publish a capture"
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
                _mkdir_private(final_path.parent, parents=True)
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
                        revision=int(workspace_projection[2]),
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

    def get_current(self, workspace_id: str) -> OdooCaptureManifest | None:
        """Return the first current manifest for legacy single-dataset callers."""

        manifests = self.get_currents(workspace_id)
        return manifests[0] if manifests else None

    def get_currents(
        self,
        workspace_id: str,
    ) -> tuple[OdooCaptureManifest, ...]:
        """Restore current protected manifests in deterministic model order."""

        manifests = tuple(
            OdooCaptureManifest.from_json(value)
            for value in self._read_json_rows(
                workspace_id,
                """
                SELECT revision.manifest_json
                  FROM odoo_capture_manifest_current AS current_manifest
                  JOIN odoo_capture_manifest_revision AS revision
                    ON revision.manifest_id = current_manifest.manifest_id
                 ORDER BY current_manifest.dataset_id
                """,
            )
        )
        return tuple(sorted(manifests, key=lambda item: item.model))

    def history(self, workspace_id: str) -> tuple[OdooCaptureManifest, ...]:
        """Restore immutable capture manifests without opening protected files."""

        return tuple(
            OdooCaptureManifest.from_json(value)
            for value in self._read_json_rows(
                workspace_id,
                """
                SELECT manifest_json
                  FROM odoo_capture_manifest_revision
                 ORDER BY captured_at, manifest_id
                """,
            )
        )

    def source_snapshot_storage_keys(self, workspace_id: str) -> frozenset[str]:
        """Return the immutable value artifacts registered by current code."""

        return frozenset(
            self._read_json_rows(
                workspace_id,
                """
                SELECT parquet_storage_key
                  FROM source_snapshot_manifest
                 ORDER BY parquet_storage_key
                """,
            )
        )

    def recover_incomplete_publications(self, workspace_id: str) -> int:
        """Remove pending and unreferenced artifacts without moving pointers."""

        manifests = self.history(workspace_id)
        referenced_provenance = {
            manifest.provenance_storage_key for manifest in manifests
        }
        removed = 0
        root = self._protected_root(workspace_id)
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
        if manifests:
            data_version_ids = {item.data_version_id for item in manifests}
            if len(data_version_ids) != 1:
                raise WorkspaceError(
                    "Odoo capture history spans more than one DataVersion"
                )
            removed += self._artifacts.cleanup_source_snapshots(
                data_version_ids.pop(),
                self.source_snapshot_storage_keys(workspace_id),
            )
        return removed

    def read_encrypted(self, workspace_id: str, manifest: OdooCaptureManifest) -> bytes:
        """Read one contained, bounded protected sidecar for service verification."""

        context_reader = getattr(self._database, "workspace_access_context", None)
        if (
            context_reader is not None
            and manifest.data_version_id
            != context_reader(workspace_id).data_version_id
        ):
            raise WorkspaceError(
                "Odoo capture manifest belongs to another DataVersion"
            )
        path = self._artifact_path(workspace_id, manifest.provenance_storage_key)
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
        workspace_id: str,
        *,
        reason: str,
        actor: Actor,
    ) -> bool:
        """Retire all current capture roots while preserving immutable history."""

        if not reason.strip() or len(reason) > 200:
            raise WorkspaceError("Odoo provenance invalidation reason is invalid")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            exists = connection.execute(
                """
                SELECT manifest_id
                  FROM odoo_capture_manifest_current
                 LIMIT 1
                """
            ).fetchone()
            if exists is None:
                return False
            revision = self._workspace_revision(connection)
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
        workspace_id: str,
        *,
        now: datetime,
        actor: Actor,
    ) -> int:
        """Delete expired non-current artifacts and their manifest revisions."""

        if now.tzinfo is None:
            raise WorkspaceError("Odoo retention time must be timezone-aware")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT manifest_id, provenance_storage_key, data_storage_key,
                       manifest_json
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
            revision = self._workspace_revision(connection)
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
        for _, storage_key, _, _ in rows:
            self._artifact_path(workspace_id, str(storage_key)).unlink(missing_ok=True)
        expired_data_version_ids = {
            OdooCaptureManifest.from_json(str(row[3])).data_version_id
            for row in rows
        }
        if len(expired_data_version_ids) != 1:
            raise WorkspaceError(
                "Expired Odoo capture history spans more than one DataVersion"
            )
        self._artifacts.cleanup_source_snapshots(
            expired_data_version_ids.pop(),
            self.source_snapshot_storage_keys(workspace_id),
        )
        return len(rows)

    def _write_candidate(self, path: Path, value: bytes) -> None:
        if len(value) > CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes:
            raise WorkspaceError("Odoo provenance candidate exceeds snapshot limit")
        _mkdir_private(path.parent, parents=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _candidate_path(self, workspace_id: str, manifest_id: str) -> Path:
        root = self._protected_root(workspace_id)
        candidates = root / "candidates"
        if candidates.is_symlink():
            raise WorkspaceError("Protected Odoo candidate directory is unsafe")
        _mkdir_private(candidates)
        if candidates.resolve().parent != root.resolve():
            raise WorkspaceError(
                "Protected Odoo candidate directory escapes its DataVersion root"
            )
        return candidates / f"{manifest_id}.pending"

    def _artifact_path(self, workspace_id: str, storage_key: str) -> Path:
        root = self._protected_root(workspace_id)
        captures = root / "captures"
        if captures.is_symlink():
            raise WorkspaceError("Protected Odoo capture directory is unsafe")
        _mkdir_private(captures)
        candidate = root / storage_key
        resolved = candidate.resolve()
        if (
            captures.resolve().parent != root.resolve()
            or resolved.parent != captures.resolve()
        ):
            raise WorkspaceError(
                "Odoo provenance storage key escapes its DataVersion root"
            )
        return candidate

    def _protected_root(self, workspace_id: str) -> Path:
        root = self._protected_root_resolver(workspace_id)
        if root.is_symlink():
            raise WorkspaceError("Protected Odoo evidence directory is unsafe")
        _mkdir_private(root, parents=True)
        return root


def _mkdir_private(path: Path, *, parents: bool = False) -> None:
    """Create a private directory without translating POSIX modes into Windows ACLs."""

    path.mkdir(parents=parents, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _validate_complete_capture(
    workspace_id: str,
    manifest: OdooCaptureManifest,
    selection: SourceSelection,
    snapshot: SourceSnapshot,
) -> None:
    _validate_complete_captures(
        workspace_id,
        (manifest,),
        selection,
        (snapshot,),
    )


def _validate_complete_captures(
    workspace_id: str,
    manifests: tuple[OdooCaptureManifest, ...],
    selection: SourceSelection,
    snapshots: tuple[SourceSnapshot, ...],
) -> None:
    """Require one internally consistent publication set for every dataset."""

    if not manifests or not snapshots or not selection.datasets:
        raise WorkspaceError("Odoo capture publication is empty")
    manifest_by_dataset = {item.dataset_id: item for item in manifests}
    snapshot_by_dataset = {item.dataset_id: item for item in snapshots}
    dataset_by_id = {item.dataset_id: item for item in selection.datasets}
    dataset_ids = set(dataset_by_id)
    if (
        len(manifest_by_dataset) != len(manifests)
        or len(snapshot_by_dataset) != len(snapshots)
        or len(dataset_by_id) != len(selection.datasets)
        or set(manifest_by_dataset) != dataset_ids
        or set(snapshot_by_dataset) != dataset_ids
        or any(
            item.data_version_id != selection.data_version_id
            for item in (*manifests, *snapshots)
        )
    ):
        raise WorkspaceError(
            "Odoo capture publication belongs to another DataVersion"
        )
    expected_selection_hash = content_hash(
        {
            "contract_version": WORKSPACE_EVIDENCE_IDENTITY_CONTRACT_VERSION,
            "data_version_id": selection.data_version_id,
            "version": selection.version,
            "datasets": [item.to_dict() for item in selection.datasets],
        }
    )
    if selection.content_hash != expected_selection_hash:
        raise WorkspaceError(
            "Odoo values, provenance, and source snapshot bindings are inconsistent"
        )
    for dataset in selection.datasets:
        manifest = manifest_by_dataset[dataset.dataset_id]
        snapshot = snapshot_by_dataset[dataset.dataset_id]
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
            dataset.source != expected_source
            or dataset.dataset_id != manifest.dataset_id
            or dataset.name != manifest.dataset_name
            or dataset.row_count != manifest.row_count
            or tuple(item.source_name for item in dataset.columns)
            != manifest.field_names
            or tuple(item.stable_key for item in dataset.columns)
            != manifest.column_stable_keys
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
