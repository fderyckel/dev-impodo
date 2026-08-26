"""Persist immutable prepared-snapshot manifests and session bindings."""

from __future__ import annotations

from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import PreparationSessionStatus
from ...workspace_errors import WorkspaceError


class PreparationSnapshotBindings:
    """Own prepared-snapshot reuse and the building-session binding boundary."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def find(
        self,
        workspace_id: str,
        dataset_id: str,
        logical_hash: str,
    ) -> PreparedSnapshot | None:
        """Find one historical exact prepared artifact for safe reuse."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT manifest_json
                  FROM prepared_snapshot_manifest
                 WHERE dataset_id = ? AND logical_hash = ?
                 ORDER BY created_at DESC, content_hash
                 LIMIT 1
                """,
                [dataset_id, logical_hash],
            ).fetchone()
        return PreparedSnapshot.from_json(str(row[0])) if row is not None else None

    def current(self, workspace_id: str) -> tuple[PreparedSnapshot, ...]:
        """Load snapshots advanced only by a fully published preparation."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT manifest.manifest_json
                  FROM prepared_snapshot_current AS current
                  JOIN prepared_snapshot_manifest AS manifest
                    ON manifest.content_hash = current.content_hash
                 ORDER BY current.dataset_id
                """
            ).fetchall()
        return tuple(PreparedSnapshot.from_json(str(row[0])) for row in rows)

    def storage_keys(self, workspace_id: str) -> frozenset[str]:
        """Return immutable prepared files referenced by any manifest."""

        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
                SELECT parquet_storage_key
                  FROM prepared_snapshot_manifest
                 ORDER BY parquet_storage_key
                """
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def bind(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
    ) -> None:
        """Register a verified manifest and bind it to one building session."""

        if snapshot.workspace_id != workspace_id:
            raise WorkspaceError("Prepared snapshot belongs to another workspace")
        canonical_session_id = self._repository._session_id(session_id)
        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with self._repository._connect(database_path) as connection:
            self._repository._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._repository._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                bindings = connection.execute(
                    """
                    SELECT mapping_hash, schema_hash
                      FROM preparation_session
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if bindings != (snapshot.mapping_hash, snapshot.schema_hash):
                    raise WorkspaceError(
                        "Prepared snapshot does not match the preparation session"
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO prepared_snapshot_manifest
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot.content_hash,
                        snapshot.dataset_id,
                        snapshot.logical_hash,
                        snapshot.source_snapshot_hash,
                        snapshot.mapping_hash,
                        snapshot.schema_hash,
                        snapshot.transformation_program_hash,
                        snapshot.row_count,
                        snapshot.parquet_sha256,
                        snapshot.parquet_storage_key,
                        snapshot.created_at.isoformat(),
                        snapshot.to_json(),
                    ],
                )
                registered = connection.execute(
                    """
                    SELECT dataset_id, logical_hash, parquet_sha256,
                           parquet_storage_key
                      FROM prepared_snapshot_manifest
                     WHERE content_hash = ?
                    """,
                    [snapshot.content_hash],
                ).fetchone()
                if registered != (
                    snapshot.dataset_id,
                    snapshot.logical_hash,
                    snapshot.parquet_sha256,
                    snapshot.parquet_storage_key,
                ):
                    raise WorkspaceError(
                        "Stored prepared snapshot manifest is inconsistent"
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO preparation_session_snapshot
                    VALUES (?, ?, ?)
                    """,
                    [
                        canonical_session_id,
                        snapshot.dataset_id,
                        snapshot.content_hash,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
