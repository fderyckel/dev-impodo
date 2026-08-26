"""Write and finalize bounded direct and native preparation sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from impodo.domain.shared.access import Actor
from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparationSessionSummary,
    PreparedCanonicalProjection,
    StoredCanonicalStagingRun,
    transformation_impact_to_portable_dict,
    transformation_report_to_portable_dict,
)
from ...domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)
from impodo.domain.shared.models import Issue, LogicalReference, canonical_json_bytes, portable_value
from impodo.domain.preparation.staging import StagingRunStatus
from impodo.domain.preparation.staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetRole,
    StagingDisposition,
    validate_canonical_row_bindings,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from .constants import (
    DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
    DUCKDB_JSON_BATCH_MAX_BYTES,
    PREPARATION_SESSION_ROW_BATCH_SIZE,
)
from .native_prepared_projection import (
    NativePreparedProjectionResult,
    append_clean_native_projection,
    supports_clean_native_projection,
)
from .preparation_session_support import (
    _CANONICAL_STAGING_ROW_JSON_STRUCTURE,
    _DIRECT_IDENTITY_JSON_STRUCTURE,
    _DIRECT_LINEAGE_JSON_STRUCTURE,
    _DIRECT_PHYSICAL_ROW_JSON_STRUCTURE,
    _DIRECT_RELATIONSHIP_JSON_STRUCTURE,
    _PREPARATION_IMPACT_JSON_STRUCTURE,
    _canonical_row_requires_scalar_transport,
)
from .serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)


class PreparationDirectWriter:
    def begin_direct_session(
        self,
        workspace_id: str,
        bindings: PreparationSessionBindings,
        *,
        actor: Actor,
    ) -> PreparationSessionSummary:
        """Create a session whose UUID is also a pending canonical run ID."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        session_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                INSERT INTO preparation_session (
                    session_id, status, mapping_id, mapping_version,
                    physical_selection_hash, source_selection_hash,
                    mapping_hash, schema_hash, derived_plan_hash,
                    compiled_plan_hash, contract_version, evaluator_version,
                    source_hashes_json, run_issues_json, control_totals_json,
                    reconciliation_json, dataset_reconciliation_json,
                    impact_report_json, staged_row_count,
                    canonical_row_count, impact_row_count, created_at,
                    updated_at, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]',
                          NULL, NULL, NULL, 0, 0, 0, ?, ?, NULL)
                    """,
                    [
                        session_id,
                        PreparationSessionStatus.BUILDING.value,
                        bindings.mapping_id,
                        bindings.mapping_version,
                        bindings.physical_selection_hash,
                        bindings.source_selection_hash,
                        bindings.mapping_hash,
                        bindings.schema_hash,
                        bindings.derived_plan_hash,
                        bindings.compiled_plan_hash,
                        bindings.contract_version,
                        bindings.evaluator_version,
                        _canonical_json(dict(sorted(bindings.source_hashes.items()))),
                        now,
                        now,
                    ],
                )
                zero_hash = "sha256:" + "0" * 64
                connection.execute(
                    """
                        INSERT INTO canonical_staging_run (
                            run_id, content_hash, mapping_id, mapping_version,
                            physical_selection_hash, source_selection_hash,
                            mapping_hash, schema_hash, derived_plan_hash,
                            compiled_plan_hash, contract_version,
                            evaluator_version, status, published_at,
                            published_by, row_count, run_issues_json,
                            reconciliation_json,
                            dataset_reconciliation_json,
                            control_totals_json, retired_at,
                            retired_reason, successor_run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  0, '[]', '{}', '[]', '[]', NULL, NULL, NULL)
                    """,
                    [
                        session_id,
                        zero_hash,
                        bindings.mapping_id,
                        bindings.mapping_version,
                        bindings.physical_selection_hash,
                        bindings.source_selection_hash,
                        bindings.mapping_hash,
                        bindings.schema_hash,
                        bindings.derived_plan_hash,
                        bindings.compiled_plan_hash,
                        bindings.contract_version,
                        bindings.evaluator_version,
                        StagingRunStatus.PENDING.value,
                        now,
                        actor.identity.display_name,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return PreparationSessionSummary(
            session_id=session_id,
            status=PreparationSessionStatus.BUILDING,
            bindings=bindings,
        )

    def append_direct_rows(
        self,
        workspace_id: str,
        session_id: str,
        rows: Sequence[CanonicalPreparedSessionRow],
    ) -> None:
        """Write one encoded batch directly into its pending canonical run."""

        if not rows:
            return
        canonical_session_id = self._session_id(session_id)
        expected_lineage_count = 0
        for item in rows:
            if not item.physical_sources:
                raise ValueError("Prepared session rows require physical lineage")
            for physical_dataset_id, source_rows in sorted(
                item.physical_sources.items()
            ):
                ordered = tuple(sorted(set(source_rows)))
                if not physical_dataset_id or ordered != tuple(source_rows):
                    raise ValueError("Prepared session lineage is invalid")
                for source_row in ordered:
                    if source_row < 1:
                        raise ValueError(
                            "Prepared session source rows must be positive"
                        )
                    expected_lineage_count += 1

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                pending = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_staging_run
                     WHERE run_id = ? AND status = ?
                    """,
                    [canonical_session_id, StagingRunStatus.PENDING.value],
                ).fetchone()
                if pending is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                json_rows = []
                scalar_rows = []
                for item in rows:
                    destination = (
                        scalar_rows
                        if _canonical_row_requires_scalar_transport(item)
                        else json_rows
                    )
                    destination.append(item)
                canonical_row_count = 0
                transport_rows = (
                    {
                        "ordinal": item.ordinal,
                        "row_id": item.row_id,
                        "dataset": item.dataset,
                        "source_row": item.source_row,
                        "target_model": item.target_model,
                        "disposition": item.disposition.value,
                        "record_label": item.record_label,
                        "quality_identity_key": item.quality_identity_key,
                        "row_json": item.row_json,
                    }
                    for item in json_rows
                )
                for encoded_batch in iter_encoded_json_batches(
                    transport_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row (
                            run_id, ordinal, row_id, dataset, source_row,
                            target_model, disposition, record_label,
                            quality_identity_key, row_json
                        )
                        SELECT
                            ?, item.ordinal, item.row_id, item.dataset,
                            item.source_row, item.target_model,
                            item.disposition, item.record_label,
                            item.quality_identity_key, item.row_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _CANONICAL_STAGING_ROW_JSON_STRUCTURE,
                        ],
                    )
                    canonical_row_count += encoded_batch.row_count
                for item in scalar_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row (
                            run_id, ordinal, row_id, dataset, source_row,
                            target_model, disposition, record_label,
                            quality_identity_key, row_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            canonical_session_id,
                            item.ordinal,
                            item.row_id,
                            item.dataset,
                            item.source_row,
                            item.target_model,
                            item.disposition.value,
                            item.record_label,
                            item.quality_identity_key,
                            item.row_json,
                        ],
                    )
                    canonical_row_count += 1
                if canonical_row_count != len(rows):
                    raise WorkspaceError("Prepared canonical row batch is incomplete")
                issue_rows = [
                    [
                        canonical_session_id,
                        item.ordinal,
                        issue_ordinal,
                        _canonical_json(issue.to_portable_dict()),
                    ]
                    for item in rows
                    for issue_ordinal, issue in enumerate(item.issues, start=1)
                ]
                if issue_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row_issue
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(issue_rows),
                    )
                identity_rows = (
                    {
                        "ordinal": item.ordinal,
                        "dataset": item.dataset,
                        "identity_hash": "sha256:"
                        + sha256(
                            canonical_json_bytes(
                                {
                                    "dataset": item.dataset,
                                    "source_identity": portable_value(
                                        item.source_identity
                                    ),
                                }
                            )
                        ).hexdigest(),
                        "base_disposition": item.disposition.value,
                        "finalized_duplicate": False,
                    }
                    for item in rows
                    if item.source_identity
                )
                identity_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    identity_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_direct_identity (
                            session_id, ordinal, dataset, identity_hash,
                            base_disposition, finalized_duplicate
                        )
                        SELECT
                            ?, item.ordinal, item.dataset,
                            item.identity_hash, item.base_disposition,
                            item.finalized_duplicate
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_IDENTITY_JSON_STRUCTURE,
                        ],
                    )
                    identity_count += encoded_batch.row_count
                if identity_count != sum(bool(item.source_identity) for item in rows):
                    raise WorkspaceError("Prepared identity fact batch is incomplete")

                relationship_rows = (
                    {
                        "child_ordinal": item.ordinal,
                        "target_field": target_field,
                        "item_ordinal": item_ordinal,
                        "parent_dataset": reference.dataset,
                        "normalized_key_json": _canonical_json(
                            portable_value(reference.key)
                        ),
                        "parent_identity_hash": "sha256:"
                        + sha256(
                            canonical_json_bytes(
                                {
                                    "dataset": reference.dataset,
                                    "source_identity": portable_value(reference.key),
                                }
                            )
                        ).hexdigest(),
                    }
                    for item in rows
                    for target_field, raw_reference in sorted(item.references.items())
                    for references in (
                        raw_reference
                        if isinstance(raw_reference, tuple)
                        else (raw_reference,),
                    )
                    for item_ordinal, reference in enumerate(references)
                    if isinstance(reference, LogicalReference)
                    and reference.origin == "incoming"
                    and reference.dataset
                )
                for encoded_batch in iter_encoded_json_batches(
                    relationship_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_relationship_edge (
                            session_id, child_ordinal, target_field,
                            item_ordinal, parent_dataset,
                            normalized_key_json, parent_identity_hash,
                            match_state, resolution_state, match_count,
                            resolved_parent_ordinal
                        )
                        SELECT
                            ?, item.child_ordinal, item.target_field,
                            item.item_ordinal,
                            item.parent_dataset, item.normalized_key_json,
                            item.parent_identity_hash, 'PENDING', 'PENDING',
                            0, NULL
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_RELATIONSHIP_JSON_STRUCTURE,
                        ],
                    )

                lineage_rows = (
                    {
                        "dataset": item.dataset,
                        "output_source_row": item.source_row,
                        "physical_dataset_id": physical_dataset_id,
                        "physical_source_row": source_row,
                    }
                    for item in rows
                    for physical_dataset_id, source_rows in sorted(
                        item.physical_sources.items()
                    )
                    for source_row in source_rows
                )
                lineage_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    lineage_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_lineage (
                            session_id, dataset, output_source_row,
                            physical_dataset_id, physical_source_row
                        )
                        SELECT
                            ?, item.dataset, item.output_source_row,
                            item.physical_dataset_id,
                            item.physical_source_row
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_LINEAGE_JSON_STRUCTURE,
                        ],
                    )
                    lineage_count += encoded_batch.row_count
                if lineage_count != expected_lineage_count:
                    raise WorkspaceError("Prepared lineage fact batch is incomplete")

                physical_rows = (
                    {
                        "physical_dataset_id": physical_dataset_id,
                        "source_row": source_row,
                    }
                    for item in rows
                    for physical_dataset_id, source_rows in sorted(
                        item.physical_sources.items()
                    )
                    for source_row in source_rows
                )
                physical_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    physical_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO preparation_physical_row (
                            session_id, physical_dataset_id, source_row
                        )
                        SELECT
                            ?, item.physical_dataset_id, item.source_row
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            canonical_session_id,
                            encoded_batch.payload,
                            _DIRECT_PHYSICAL_ROW_JSON_STRUCTURE,
                        ],
                    )
                    physical_count += encoded_batch.row_count
                if physical_count != expected_lineage_count:
                    raise WorkspaceError(
                        "Prepared physical-row fact batch is incomplete"
                    )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET staged_row_count = staged_row_count + ?,
                           updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        canonical_row_count,
                        datetime.now(timezone.utc).isoformat(),
                        canonical_session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def append_native_prepared_projection(
        self,
        workspace_id: str,
        session_id: str,
        snapshot: PreparedSnapshot,
        projection: PreparedCanonicalProjection,
        path,
        control_fields: tuple[str, ...] = (),
    ) -> NativePreparedProjectionResult | None:
        """Project one complete clean native dataset without Python row objects."""

        if not projection.set_based_projection:
            raise ValueError("Native projection route metadata is required")
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect_prepared(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            if not supports_clean_native_projection(
                connection,
                path,
                projection.program,
                control_fields,
            ):
                return None

        self.bind_prepared_canonical_projection(
            workspace_id,
            session_id,
            snapshot,
            projection,
        )
        canonical_session_id = self._session_id(session_id)
        with self._connect_prepared(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.BUILDING,
                )
                pending = connection.execute(
                    """
                    SELECT 1
                      FROM canonical_staging_run
                     WHERE run_id = ? AND status = ?
                    """,
                    [canonical_session_id, StagingRunStatus.PENDING.value],
                ).fetchone()
                if pending is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                result = append_clean_native_projection(
                    connection,
                    path=path,
                    session_id=canonical_session_id,
                    projection=projection,
                    control_fields=control_fields,
                )
                observed = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ? AND ordinal >= ? AND ordinal < ?
                    """,
                    [
                        canonical_session_id,
                        projection.ordinal_start,
                        projection.ordinal_start + projection.row_count,
                    ],
                ).fetchone()
                if observed is None or int(observed[0]) != projection.row_count:
                    raise WorkspaceError("Native canonical row index is incomplete")
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def append_impacts(
        self,
        workspace_id: str,
        session_id: str,
        rows: Sequence[TransformationImpactRow],
    ) -> None:
        """Append one bounded impact batch with deterministic ordinals."""

        if not rows:
            return
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.BUILDING,
                )
                impact_start = int(current[2])
                impact_rows = (
                    {
                        "ordinal": impact_start + offset,
                        "dataset": row.dataset,
                        "source_row": row.source_row,
                        "target_field": row.target_field,
                        "outcome": row.outcome,
                        "impact_json": _canonical_json(
                            transformation_impact_to_portable_dict(row)
                        ),
                    }
                    for offset, row in enumerate(rows)
                )
                impact_count = 0
                for encoded_batch in iter_encoded_json_batches(
                    impact_rows,
                    max_rows=PREPARATION_SESSION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO preparation_impact_row (
                            session_id, ordinal, dataset, source_row,
                            target_field, outcome, impact_json
                        )
                        SELECT
                            ?, item.ordinal, item.dataset, item.source_row,
                            item.target_field, item.outcome, item.impact_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            session_id,
                            encoded_batch.payload,
                            _PREPARATION_IMPACT_JSON_STRUCTURE,
                        ],
                    )
                    impact_count += encoded_batch.row_count
                if impact_count != len(rows):
                    raise WorkspaceError("Preparation impact batch is incomplete")
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET impact_row_count = impact_row_count + ?,
                           updated_at = ?
                     WHERE session_id = ?
                    """,
                    [
                        impact_count,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def finalize_direct_session(
        self,
        workspace_id: str,
        session_id: str,
        *,
        dataset_evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
        run_issues: Sequence[Issue],
        control_totals: Sequence[CanonicalControlTotal],
        impact_report: TransformationImpactReport,
    ) -> StoredCanonicalStagingRun:
        """Finalize rows already stored under a pending canonical run ID."""

        summary = self.get_session(workspace_id, session_id)
        if summary.status not in {
            PreparationSessionStatus.BUILDING,
            PreparationSessionStatus.FINALIZING,
        }:
            raise WorkspaceError("Preparation session cannot be finalized")
        canonical_session_id = self._session_id(session_id)
        self._restart_direct_finalization(workspace_id, canonical_session_id)
        bindings = summary.bindings
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            collision_counts = {
                (str(dataset), str(identity_hash)): int(identity_count)
                for dataset, identity_hash, identity_count in connection.execute(
                    """
                    SELECT dataset, identity_hash, identity_count
                      FROM preparation_identity_group
                     WHERE session_id = ? AND identity_count > 1
                    """,
                    [canonical_session_id],
                ).fetchall()
            }

        duplicate_rows: list[tuple[int, int]] = []
        with self._connect(database_path) as connection:
            cursor = connection.execute(
                """
                SELECT ordinal, dataset, identity_hash,
                       finalized_duplicate
                  FROM preparation_direct_identity
                 WHERE session_id = ?
                 ORDER BY ordinal
                """,
                [canonical_session_id],
            )
            while identity_batch := cursor.fetchmany(
                PREPARATION_SESSION_ROW_BATCH_SIZE
            ):
                duplicate_rows.extend(
                    (
                        int(ordinal),
                        collision_counts[(str(dataset), str(identity_hash))],
                    )
                    for ordinal, dataset, identity_hash, finalized in identity_batch
                    if not bool(finalized)
                    and (
                        str(dataset),
                        str(identity_hash),
                    )
                    in collision_counts
                )

        canonical_issues = [CanonicalIssue.from_issue(item) for item in run_issues]
        for start in range(
            0,
            len(duplicate_rows),
            PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            duplicate_batch = duplicate_rows[
                start : start + PREPARATION_SESSION_ROW_BATCH_SIZE
            ]
            duplicate_counts_by_ordinal = dict(duplicate_batch)
            ordinals = [item[0] for item in duplicate_batch]
            rows_by_ordinal = self._direct_rows_by_ordinal(
                workspace_id,
                canonical_session_id,
                ordinals,
            )
            if len(rows_by_ordinal) != len(ordinals):
                raise WorkspaceError("Direct duplicate rows are incomplete")
            values: list[list[object]] = []
            for ordinal in ordinals:
                identity_count = duplicate_counts_by_ordinal[ordinal]
                row = rows_by_ordinal[ordinal]
                duplicate = Issue(
                    code="SOURCE_IDENTITY_DUPLICATE",
                    message=(
                        f"source identity {row.source_identity!r} occurs "
                        f"{int(identity_count)} times"
                    ),
                    dataset=row.dataset,
                    row=row.source_row,
                    affected_count=int(identity_count),
                )
                canonical_duplicate = CanonicalIssue.from_issue(duplicate)
                canonical_issues.append(canonical_duplicate)
                row = replace(
                    row,
                    disposition=StagingDisposition.BLOCKED,
                    issues=(*row.issues, canonical_duplicate),
                )
                validate_canonical_row_bindings(
                    row,
                    source_selection_hash=bindings.source_selection_hash,
                    mapping_hash=bindings.mapping_hash,
                    schema_hash=bindings.schema_hash,
                    derived_plan_hash=bindings.derived_plan_hash,
                )
                values.append(
                    [
                        int(ordinal),
                        row.disposition.value,
                        _canonical_json(row.to_portable_dict()),
                        True,
                    ]
                )
            self._update_direct_rows(
                workspace_id,
                canonical_session_id,
                values,
            )

        self._resolve_relationship_edges(
            workspace_id,
            canonical_session_id,
        )

        reconciliation, datasets = self._reconciliation(
            workspace_id,
            canonical_session_id,
            dataset_evidence,
        )
        controls = tuple(sorted(control_totals, key=lambda item: item.control_id))
        content_hash, row_count = self._hash_direct_run(
            workspace_id=workspace_id,
            run_id=canonical_session_id,
            bindings=bindings,
            datasets=datasets,
            issues=tuple(canonical_issues),
            reconciliation=reconciliation,
            control_totals=controls,
        )
        if row_count != summary.staged_row_count:
            raise WorkspaceError(
                "Direct preparation rows were not finalized completely"
            )
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                self._require_status(
                    connection,
                    canonical_session_id,
                    PreparationSessionStatus.FINALIZING,
                )
                pending = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()
                if pending is None or int(pending[0]) != row_count:
                    raise WorkspaceError("Direct preparation rows are incomplete")
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, canonical_row_count = ?,
                           run_issues_json = ?, control_totals_json = ?,
                           reconciliation_json = ?,
                           dataset_reconciliation_json = ?,
                           impact_report_json = ?, updated_at = ?,
                           failure_code = NULL
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.READY.value,
                        row_count,
                        _canonical_json(
                            [item.to_portable_dict() for item in canonical_issues]
                        ),
                        _canonical_json([item.to_portable_dict() for item in controls]),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json([item.to_portable_dict() for item in datasets]),
                        _canonical_json(
                            transformation_report_to_portable_dict(impact_report)
                        ),
                        now,
                        canonical_session_id,
                    ],
                )
                updated = connection.execute(
                    """
                    UPDATE canonical_staging_run
                       SET content_hash = ?, row_count = ?,
                           run_issues_json = ?, reconciliation_json = ?,
                           dataset_reconciliation_json = ?,
                           control_totals_json = ?
                     WHERE run_id = ? AND status = ?
                    RETURNING run_id
                    """,
                    [
                        content_hash,
                        row_count,
                        _canonical_json(
                            [item.to_portable_dict() for item in canonical_issues]
                        ),
                        _canonical_json(reconciliation.to_portable_dict()),
                        _canonical_json([item.to_portable_dict() for item in datasets]),
                        _canonical_json([item.to_portable_dict() for item in controls]),
                        canonical_session_id,
                        StagingRunStatus.PENDING.value,
                    ],
                ).fetchone()
                if updated is None:
                    raise WorkspaceError("Direct preparation run is not pending")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.load_stored_run(workspace_id, canonical_session_id)

    def _restart_direct_finalization(
        self,
        workspace_id: str,
        session_id: str,
    ) -> None:
        """Rebuild compact identity groups and safely resume duplicate edits."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        while True:
            with self._connect(database_path) as connection:
                identity_batch = connection.execute(
                    """
                    SELECT ordinal, base_disposition
                      FROM preparation_direct_identity
                     WHERE session_id = ? AND finalized_duplicate
                     ORDER BY ordinal
                     LIMIT ?
                    """,
                    [session_id, PREPARATION_SESSION_ROW_BATCH_SIZE],
                ).fetchall()
            if not identity_batch:
                break
            base_by_ordinal = {
                int(ordinal): str(base_disposition)
                for ordinal, base_disposition in identity_batch
            }
            ordinals = list(base_by_ordinal)
            rows_by_ordinal = self._direct_rows_by_ordinal(
                workspace_id,
                session_id,
                ordinals,
            )
            if len(rows_by_ordinal) != len(ordinals):
                raise WorkspaceError("Direct duplicate rows are incomplete")
            values: list[list[object]] = []
            for ordinal in ordinals:
                row = replace(
                    rows_by_ordinal[ordinal],
                    disposition=StagingDisposition(base_by_ordinal[ordinal]),
                    issues=tuple(
                        issue
                        for issue in rows_by_ordinal[ordinal].issues
                        if issue.code != "SOURCE_IDENTITY_DUPLICATE"
                    ),
                )
                values.append(
                    [
                        int(ordinal),
                        row.disposition.value,
                        _canonical_json(row.to_portable_dict()),
                        False,
                    ]
                )
            self._update_direct_rows(workspace_id, session_id, values)

        with self._connect(database_path) as connection:
            connection.begin()
            try:
                status = connection.execute(
                    "SELECT status FROM preparation_session WHERE session_id = ?",
                    [session_id],
                ).fetchone()
                if status is None or str(status[0]) not in {
                    PreparationSessionStatus.BUILDING.value,
                    PreparationSessionStatus.FINALIZING.value,
                }:
                    raise WorkspaceError("Preparation session cannot be finalized")
                connection.execute(
                    "DELETE FROM preparation_identity_group WHERE session_id = ?",
                    [session_id],
                )
                connection.execute(
                    """
                    INSERT INTO preparation_identity_group (
                        session_id, dataset, identity_hash, identity_count
                    )
                    SELECT session_id, dataset, identity_hash, COUNT(*)
                      FROM preparation_direct_identity
                     WHERE session_id = ?
                     GROUP BY session_id, dataset, identity_hash
                    """,
                    [session_id],
                )
                connection.execute(
                    """
                    UPDATE preparation_session
                       SET status = ?, canonical_row_count = 0,
                           updated_at = ?, failure_code = NULL
                     WHERE session_id = ?
                    """,
                    [
                        PreparationSessionStatus.FINALIZING.value,
                        datetime.now(timezone.utc).isoformat(),
                        session_id,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _update_direct_rows(
        self,
        workspace_id: str,
        run_id: str,
        values: Sequence[Sequence[object]],
    ) -> None:
        if not values:
            return
        overlay_rows: list[list[object]] = []
        ordinals = [int(item[0]) for item in values]
        for ordinal, _disposition, row_text, finalized in values:
            if not bool(finalized):
                continue
            try:
                row = CanonicalRow.from_dict(json.loads(str(row_text)))
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Stored direct preparation row is invalid"
                ) from error
            duplicate_issues = tuple(
                issue
                for issue in row.issues
                if issue.code == "SOURCE_IDENTITY_DUPLICATE"
            )
            if len(duplicate_issues) != 1:
                raise WorkspaceError("Direct duplicate issue evidence is invalid")
            overlay_rows.append(
                [
                    run_id,
                    int(ordinal),
                    0,
                    _canonical_json(duplicate_issues[0].to_portable_dict()),
                ]
            )
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                connection.execute(
                    """
                    UPDATE canonical_staging_row AS target
                       SET disposition = updates.disposition,
                           row_json = CASE
                               WHEN target.row_json = '' THEN ''
                               ELSE updates.row_json
                           END
                      FROM (
                          SELECT unnest(?) AS ordinal,
                                 unnest(?) AS disposition,
                                 unnest(?) AS row_json,
                                 unnest(?) AS finalized_duplicate
                      ) AS updates
                     WHERE target.run_id = ?
                       AND target.ordinal = updates.ordinal
                    """,
                    [*_columnar_parameters(values), run_id],
                )
                connection.execute(
                    """
                    DELETE FROM canonical_staging_row_issue
                     WHERE run_id = ? AND issue_ordinal = 0
                       AND ordinal IN (SELECT unnest(?))
                    """,
                    [run_id, ordinals],
                )
                if overlay_rows:
                    connection.execute(
                        """
                        INSERT INTO canonical_staging_row_issue
                        SELECT unnest(?), unnest(?), unnest(?), unnest(?)
                        """,
                        _columnar_parameters(overlay_rows),
                    )
                connection.execute(
                    """
                    UPDATE preparation_direct_identity AS target
                       SET finalized_duplicate = updates.finalized_duplicate
                      FROM (
                          SELECT unnest(?) AS ordinal,
                                 unnest(?) AS disposition,
                                 unnest(?) AS row_json,
                                 unnest(?) AS finalized_duplicate
                      ) AS updates
                     WHERE target.session_id = ?
                       AND target.ordinal = updates.ordinal
                    """,
                    [*_columnar_parameters(values), run_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
