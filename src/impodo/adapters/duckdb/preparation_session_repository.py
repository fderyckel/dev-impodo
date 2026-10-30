"""Stable facade for bounded, unpublished preparation-session persistence."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.shared.artifacts import WorkspaceArtifactStore

from ...domain.derived_value_artifact import DerivedValueArtifact
from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.source_snapshot import SOURCE_ROW_COLUMN, SourceSnapshot
from ...domain.staging.preparation_session import (
    PreparationSessionStatus,
    PreparationSessionSummary,
    PreparedCanonicalProjection,
)
from ...domain.workspace.errors import WorkspaceError
from .constants import (
    NATIVE_PREPARED_PROJECTION_MEMORY_LIMIT,
    PREPARATION_SESSION_MEMORY_LIMIT,
)
from .preparation_canonical_projection_bindings import (
    PreparationCanonicalProjectionBindings,
)
from .preparation_derived_artifact_bindings import PreparationDerivedArtifactBindings
from .preparation_direct_writer import PreparationDirectWriter
from .preparation_failure_cleanup import PreparationFailureCleanup
from .preparation_normalization_records import PreparationNormalizationRecords
from .preparation_quality_index import PreparationQualityIndex
from .preparation_session_lifecycle import PreparationSessionLifecycle
from .preparation_snapshot_bindings import PreparationSnapshotBindings
from .preparation_stored_run_reader import PreparationStoredRunReader
from .repository import DuckDbRepository
from .unit_of_work import retain_databases_for_operation


class PreparationSessionRepository(
    DuckDbRepository,
    PreparationDirectWriter,
    PreparationQualityIndex,
    PreparationNormalizationRecords,
    PreparationStoredRunReader,
    PreparationFailureCleanup,
):
    """Delegate public ports to focused transaction-preserving collaborators."""

    def __init__(
        self,
        database,
        artifacts: WorkspaceArtifactStore | None = None,
    ) -> None:
        super().__init__(database)
        self._artifacts = artifacts or LocalArtifactStore(database.root)
        self._prepared_snapshots = PreparationSnapshotBindings(self)
        self._derived_value_artifacts = PreparationDerivedArtifactBindings(self)
        self._canonical_projection_bindings = PreparationCanonicalProjectionBindings(
            self
        )
        self._lifecycle = PreparationSessionLifecycle(self)

    def operation_scope(self) -> AbstractContextManager[None]:
        """Release all producer-phase owners before another preparation phase."""

        return retain_databases_for_operation()

    def register_physical_snapshot(
        self, workspace_id: str, session_id: str, snapshot: SourceSnapshot, path: Path,
    ) -> None:
        """Scan only source ordinals once per snapshot into durable accounting."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect_prepared(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._require_status(connection, session_id, PreparationSessionStatus.BUILDING)
                connection.execute(
                    f"""
                    INSERT INTO preparation_physical_row
                    SELECT ?, ?, "{SOURCE_ROW_COLUMN}"
                      FROM read_parquet(?)
                    """,
                    [session_id, snapshot.dataset_id, str(path)],
                )
                count = connection.execute(
                    """SELECT COUNT(*) FROM preparation_physical_row
                        WHERE session_id = ? AND physical_dataset_id = ?""",
                    [session_id, snapshot.dataset_id],
                ).fetchone()
                if count is None or int(count[0]) != snapshot.row_count:
                    raise WorkspaceError("Frozen physical accounting is incomplete")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _connect(self, path):
        """Use a smaller hardened buffer allowance for bounded session work."""

        return self._database.connection_factory.connect(
            path,
            memory_limit=PREPARATION_SESSION_MEMORY_LIMIT,
            threads="1",
            preserve_insertion_order=False,
        )

    def _connect_prepared(self, path):
        """Allow only internal hash-verified prepared Parquet scans."""

        return self._database.connection_factory.connect(
            path,
            memory_limit=NATIVE_PREPARED_PROJECTION_MEMORY_LIMIT,
            threads="1",
            preserve_insertion_order=False,
            enable_external_access=True,
        )

    def find_prepared_snapshot(
        self,
        workspace_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> PreparedSnapshot | None:
        return self._prepared_snapshots.find(workspace_id, dataset_id, logical_hash)

    def current_prepared_snapshots(
        self,
        workspace_id: str,
    ) -> tuple[PreparedSnapshot, ...]:
        return self._prepared_snapshots.current(workspace_id)

    def prepared_snapshot_storage_keys(self, workspace_id: str) -> frozenset[str]:
        return self._prepared_snapshots.storage_keys(workspace_id)

    def bind_prepared_snapshot(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
    ) -> None:
        self._prepared_snapshots.bind(workspace_id, session_id, snapshot)

    def find_derived_value_artifact(
        self,
        workspace_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> DerivedValueArtifact | None:
        return self._derived_value_artifacts.find(
            workspace_id,
            dataset_id,
            logical_hash,
        )

    def current_derived_value_artifacts(
        self,
        workspace_id: str,
    ) -> tuple[DerivedValueArtifact, ...]:
        return self._derived_value_artifacts.current(workspace_id)

    def session_derived_value_artifacts(
        self,
        workspace_id: str,
        session_id: str,
    ) -> tuple[DerivedValueArtifact, ...]:
        return self._derived_value_artifacts.session(workspace_id, session_id)

    def derived_value_artifact_storage_keys(
        self,
        workspace_id: str,
    ) -> frozenset[str]:
        return self._derived_value_artifacts.storage_keys(workspace_id)

    def bind_derived_value_artifact(
        self,
        workspace_id: str,
        session_id: str,
        artifact: DerivedValueArtifact,
    ) -> None:
        self._derived_value_artifacts.bind(workspace_id, session_id, artifact)

    def bind_prepared_canonical_projection(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
    ) -> None:
        self._canonical_projection_bindings.bind(
            workspace_id,
            session_id,
            snapshot,
            projection,
        )

    def get_session(
        self,
        workspace_id: str,
        session_id: str,
    ) -> PreparationSessionSummary:
        return self._lifecycle.get(workspace_id, session_id)

    def mark_published(self, workspace_id: str, session_id: str) -> None:
        self._lifecycle.mark_published(workspace_id, session_id)

    def fail_session(
        self,
        workspace_id: str,
        session_id: str,
        failure_code: str,
    ) -> None:
        self._lifecycle.fail(workspace_id, session_id, failure_code)
