"""Bind native canonical projections to verified preparation snapshots."""

from __future__ import annotations

import json

from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import (
    PreparedCanonicalProjection,
    PreparationSessionStatus,
)
from ...workspace_errors import WorkspaceError
from .serialization import _canonical_json


class PreparationCanonicalProjectionBindings:
    """Own the atomic binding of native direct evidence to one session."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def bind(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
    ) -> None:
        """Bind one native direct dataset to its immutable value carrier."""

        if (
            snapshot.workspace_id != workspace_id
            or snapshot.dataset_id != projection.dataset_id
            or snapshot.row_count != projection.row_count
            or snapshot.transformation_program_hash
            != projection.program.content_hash
        ):
            raise WorkspaceError(
                "Prepared canonical projection does not match its snapshot"
            )
        canonical_session_id = self._repository._session_id(session_id)
        database_path = (
            self._repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        encoded = _canonical_json(projection.to_portable_dict())
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
                    SELECT source_selection_hash, mapping_hash, schema_hash,
                           source_hashes_json
                      FROM preparation_session
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if bindings is None:
                    raise WorkspaceError("Preparation session was not found")
                try:
                    source_hashes = json.loads(str(bindings[3]))
                except (TypeError, ValueError) as error:
                    raise WorkspaceError(
                        "Preparation session source bindings are invalid"
                    ) from error
                if (
                    projection.program.source_selection_hash != str(bindings[0])
                    or projection.program.mapping_content_hash != str(bindings[1])
                    or projection.program.schema_hash != str(bindings[2])
                    or not isinstance(source_hashes, dict)
                    or source_hashes.get(projection.dataset)
                    != projection.source_hash
                ):
                    raise WorkspaceError(
                        "Prepared canonical projection bindings changed"
                    )
                snapshot_binding = connection.execute(
                    """
                    SELECT 1
                      FROM preparation_session_snapshot
                     WHERE session_id = ? AND dataset_id = ?
                       AND content_hash = ?
                    """,
                    [
                        canonical_session_id,
                        snapshot.dataset_id,
                        snapshot.content_hash,
                    ],
                ).fetchone()
                if snapshot_binding is None:
                    raise WorkspaceError(
                        "Prepared snapshot is not bound to the session"
                    )
                overlap = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_prepared_projection
                     WHERE run_id = ?
                       AND ordinal_start < ?
                       AND ordinal_start + row_count > ?
                    """,
                    [
                        canonical_session_id,
                        projection.ordinal_start + projection.row_count,
                        projection.ordinal_start,
                    ],
                ).fetchone()
                if overlap is not None:
                    raise WorkspaceError(
                        "Prepared canonical projection ordinals overlap"
                    )
                connection.execute(
                    """
                    INSERT INTO canonical_prepared_projection
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        canonical_session_id,
                        projection.dataset_id,
                        projection.dataset,
                        projection.ordinal_start,
                        projection.row_count,
                        snapshot.content_hash,
                        encoded,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
