"""Reconstruct only requested review records from verified native snapshots."""

from __future__ import annotations

import json
from collections.abc import Sequence

import duckdb

from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.domain.workspace.errors import WorkspaceError

from .constants import PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT
from .native_prepared_projection import projected_encoded_rows_sql


def read_projected_review_rows(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    row_ids: Sequence[str],
    *,
    workspace_id: str,
    artifacts: WorkspaceArtifactStore,
) -> dict[str, CanonicalRow]:
    """Project selected ordinals with no whole-run semantic fallback."""

    placeholders = ", ".join("?" for _ in row_ids)
    bindings = connection.execute(
        f"""
        SELECT projection.projection_json, manifest.manifest_json,
               list(row.ordinal ORDER BY row.ordinal)
          FROM canonical_staging_row AS row
          JOIN canonical_prepared_projection AS projection
            ON projection.run_id = row.run_id
           AND row.ordinal >= projection.ordinal_start
           AND row.ordinal < projection.ordinal_start + projection.row_count
          JOIN prepared_snapshot_manifest AS manifest
            ON manifest.content_hash = projection.prepared_snapshot_hash
           AND manifest.dataset_id = projection.dataset_id
         WHERE row.run_id = ? AND row.row_id IN ({placeholders})
         GROUP BY projection.projection_json, manifest.manifest_json
        """,
        [run_id, *row_ids],
    ).fetchall()
    result = {}
    for projection_json, snapshot_json, ordinals in bindings:
        projection = PreparedCanonicalProjection.from_portable_dict(
            json.loads(str(projection_json))
        )
        snapshot = PreparedSnapshot.from_json(str(snapshot_json))
        if (
            snapshot.workspace_id != workspace_id
            or snapshot.dataset_id != projection.dataset_id
            or snapshot.row_count != projection.row_count
            or snapshot.transformation_program_hash != projection.program.content_hash
            or snapshot.mapping_hash != projection.program.mapping_content_hash
            or snapshot.schema_hash != projection.program.schema_hash
        ):
            raise WorkspaceError("Prepared review projection bindings changed")
        local_ordinals = [
            int(ordinal) - projection.ordinal_start for ordinal in ordinals
        ]
        # Verify the file before creating a temporary SQL owner, which never
        # opens application state or survives this synchronous page read.
        with (
            artifacts.materialize_prepared_snapshot(
                workspace_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ) as path,
            duckdb.connect(
                ":memory:",
                config={
                    "allow_community_extensions": "false",
                    "autoinstall_known_extensions": "false",
                    "autoload_known_extensions": "false",
                    "enable_external_access": "true",
                    "lock_configuration": "true",
                    "memory_limit": PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT,
                    "threads": "1",
                },
            ) as projector,
        ):
            query = projected_encoded_rows_sql(
                projection, prepared_ordinals=local_ordinals
            )
            rows = projector.execute(query, [str(path), *local_ordinals]).fetchall()
        for *_metadata, payload in rows:
            row = CanonicalRow.from_dict(json.loads(str(payload)))
            if row.row_id not in row_ids:
                raise WorkspaceError("Prepared review record identity changed")
            result[row.row_id] = row
    if set(result) != set(row_ids):
        raise WorkspaceError("Prepared review records are incomplete")
    return result
