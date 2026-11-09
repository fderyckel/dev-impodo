"""Reconstruct only requested review records from verified native snapshots."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import duckdb

from impodo.adapters.polars_transformation import iter_polars_prepared_batches
from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.preparation.staging_contracts import (
    CanonicalIssue,
    CanonicalRow,
    StagingDisposition,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
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
               list(struct_pack(
                   ordinal := row.ordinal,
                   row_id := row.row_id,
                   source_row := row.source_row,
                   disposition := row.disposition,
                   issues := overlays.issues
               ) ORDER BY row.ordinal)
          FROM canonical_staging_row AS row
          JOIN canonical_prepared_projection AS projection
            ON projection.run_id = row.run_id
           AND row.ordinal >= projection.ordinal_start
           AND row.ordinal < projection.ordinal_start + projection.row_count
          JOIN prepared_snapshot_manifest AS manifest
            ON manifest.content_hash = projection.prepared_snapshot_hash
           AND manifest.dataset_id = projection.dataset_id
          LEFT JOIN LATERAL (
              SELECT list(issue.issue_json ORDER BY issue.issue_ordinal) AS issues
                FROM canonical_staging_row_issue AS issue
               WHERE issue.run_id = row.run_id AND issue.ordinal = row.ordinal
          ) AS overlays ON TRUE
         WHERE row.run_id = ? AND row.row_id IN ({placeholders})
         GROUP BY projection.projection_json, manifest.manifest_json
        """,
        [run_id, *row_ids],
    ).fetchall()
    result = {}
    for projection_json, snapshot_json, members in bindings:
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
        coordinates = {int(member["ordinal"]): member for member in members}
        local_ordinals = tuple(ordinal - projection.ordinal_start for ordinal in coordinates)
        # Verify the file before creating a temporary SQL owner, which never
        # opens application state or survives this synchronous page read.
        with artifacts.materialize_prepared_snapshot(
                workspace_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
        ) as path:
            rows = _project_review_rows(path, projection, snapshot, local_ordinals)
        for ordinal, row in rows:
            expected = coordinates.get(ordinal)
            if expected is None or (row.row_id, row.source_row) != (
                str(expected["row_id"]), int(expected["source_row"]),
            ):
                raise WorkspaceError("Prepared review record identity changed")
            issues = tuple(
                CanonicalIssue.from_dict(json.loads(str(payload)))
                for payload in expected["issues"] or ()
            )
            result[row.row_id] = replace(
                row, disposition=StagingDisposition(str(expected["disposition"])),
                issues=(*row.issues, *issues),
            )
    if set(result) != set(row_ids):
        raise WorkspaceError("Prepared review records are incomplete")
    return result


def _project_review_rows(
    path: Path, projection: PreparedCanonicalProjection,
    snapshot: PreparedSnapshot, local_ordinals: tuple[int, ...],
) -> tuple[tuple[int, CanonicalRow], ...]:
    if projection.set_based_projection:
        with duckdb.connect(
            ":memory:", config={
                "allow_community_extensions": "false",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "enable_external_access": "true",
                "lock_configuration": "true",
                "memory_limit": PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT,
                "threads": "1",
            },
        ) as projector:
            query = projected_encoded_rows_sql(projection, prepared_ordinals=local_ordinals)
            return tuple(
                (int(ordinal), CanonicalRow.from_dict(json.loads(str(payload))))
                for ordinal, *_metadata, payload in projector.execute(query, [str(path), *local_ordinals]).fetchall()
            )
    coordinates = iter(local_ordinals)
    result = []
    for batch in iter_polars_prepared_batches(
        path, snapshot, None, projection.program, prepared_ordinals=local_ordinals,
        materialize_records=False, collect_impacts=False,
    ):
        for source_row, source_identity, target_identity, target_scope, scalar_values, references, issues in zip(
            batch.source_rows, batch.source_identities, batch.target_identities,
            batch.target_scopes, batch.scalar_values, batch.references, batch.issues, strict=True,
        ):
            ordinal = projection.ordinal_start + next(coordinates)
            encoded = canonical_prepared_session_row(
                dataset=projection.dataset, source_row=source_row,
                target_model=projection.program.target_model, source_identity=source_identity,
                target_identity=target_identity, target_scope=target_scope,
                scalar_values=scalar_values, references=references, issues=issues,
                ordinal=ordinal, mode=projection.mode, source_hash=projection.source_hash,
                source_selection_hash=projection.program.source_selection_hash,
                mapping_hash=projection.program.mapping_content_hash,
                schema_hash=projection.program.schema_hash, field_sources=projection.field_sources,
                physical_dataset_id=projection.physical_dataset_id,
            )
            result.append((ordinal, CanonicalRow.from_dict(json.loads(encoded.row_json))))
    return tuple(result)
