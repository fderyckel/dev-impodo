"""Read, hash, and reconstruct bounded stored canonical runs."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace

import duckdb

from ...artifacts import ArtifactStoreError
from ...domain.derived_value_artifact import DerivedValueArtifact
from ...domain.prepared_snapshot import PreparedSnapshot
from ...domain.serialization import CanonicalJsonObjectHasher
from ...domain.staging.canonical_projection import canonical_prepared_session_row
from ...domain.staging.preparation_session import (
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparedCanonicalProjection,
    StoredCanonicalStagingRun,
)
from ...staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetReconciliation,
    StagingDatasetRole,
    StagingDisposition,
    StagingReconciliation,
)
from ...workspace_errors import WorkspaceError
from ..polars_transformation import iter_polars_prepared_batches
from .constants import (
    PREPARATION_SESSION_ROW_BATCH_SIZE,
    PREPARED_VALUE_PROJECTOR_MEMORY_LIMIT,
    STAGING_ROW_BATCH_SIZE,
)
from .native_prepared_projection import (
    projected_encoded_rows_sql,
)
from .preparation_session_support import (
    _STAGING_DISPOSITIONS,
    _SessionCanonicalRows,
    canonical_preparation_session_id,
)
from .serialization import (
    _canonical_json,
)


class PreparationStoredRunReader:
    def load_stored_run(
        self,
        workspace_id: str,
        session_id: str,
    ) -> StoredCanonicalStagingRun:
        """Return a READY header backed by bounded durable row slices."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT status, mapping_id, physical_selection_hash,
                       source_selection_hash, mapping_hash, schema_hash,
                       derived_plan_hash, compiled_plan_hash,
                       contract_version, evaluator_version,
                       run_issues_json, reconciliation_json,
                       dataset_reconciliation_json, control_totals_json,
                       canonical_row_count
                  FROM preparation_session
                 WHERE session_id = ?
                """,
                [self._session_id(session_id)],
            ).fetchone()
            direct = connection.execute(
                """
                SELECT content_hash
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [self._session_id(session_id)],
            ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        if direct is None:
            raise WorkspaceError("Preparation session has no canonical run")
        if PreparationSessionStatus(str(row[0])) is not PreparationSessionStatus.READY:
            raise WorkspaceError("Preparation session is not ready")
        try:
            issues = tuple(
                CanonicalIssue.from_dict(item) for item in json.loads(str(row[10]))
            )
            reconciliation = StagingReconciliation.from_dict(json.loads(str(row[11])))
            datasets = tuple(
                StagingDatasetReconciliation.from_dict(item)
                for item in json.loads(str(row[12]))
            )
            controls = tuple(
                CanonicalControlTotal.from_dict(item)
                for item in json.loads(str(row[13]))
            )
            row_count = int(row[14])
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Preparation session header is invalid") from error
        return StoredCanonicalStagingRun(
            workspace_id=workspace_id,
            mapping_id=str(row[1]),
            physical_selection_hash=str(row[2]),
            source_selection_hash=str(row[3]),
            mapping_hash=str(row[4]),
            schema_hash=str(row[5]),
            derived_plan_hash=str(row[6]) if row[6] else None,
            datasets=datasets,
            rows=_SessionCanonicalRows(
                self,
                workspace_id,
                session_id,
                row_count,
            ),
            issues=issues,
            reconciliation=reconciliation,
            compiled_plan_hash=str(row[7]),
            control_totals=controls,
            evaluator_version=int(row[9]),
            contract_version=int(row[8]),
            validated_content_hash=str(direct[0]),
        )

    def _reconciliation(
        self,
        workspace_id: str,
        session_id: str,
        evidence: Mapping[
            str,
            tuple[str, StagingDatasetRole, int, str],
        ],
    ) -> tuple[StagingReconciliation, tuple[StagingDatasetReconciliation, ...]]:
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            disposition_rows = connection.execute(
                """
                SELECT dataset, disposition, COUNT(*)
                  FROM canonical_staging_row
                 WHERE run_id = ?
                 GROUP BY dataset, disposition
                """,
                [session_id],
            ).fetchall()
            lineage_rows = connection.execute(
                """
                SELECT dataset,
                       COUNT(*),
                       COUNT(DISTINCT (physical_dataset_id, physical_source_row))
                  FROM preparation_lineage
                 WHERE session_id = ?
                 GROUP BY dataset
                """,
                [session_id],
            ).fetchall()
        dispositions: dict[str, dict[StagingDisposition, int]] = {}
        for dataset, disposition, count in disposition_rows:
            dispositions.setdefault(str(dataset), {})[
                StagingDisposition(str(disposition))
            ] = int(count)
        lineage = {
            str(dataset): (int(link_count), int(used_count))
            for dataset, link_count, used_count in lineage_rows
        }
        datasets: list[StagingDatasetReconciliation] = []
        run_counts = dict.fromkeys(StagingDisposition, 0)
        for dataset, (
            physical_dataset_id,
            role,
            input_rows,
            target_model,
        ) in sorted(evidence.items()):
            counts = dispositions.get(dataset, {})
            output_rows = sum(counts.values())
            lineage_links, input_rows_used = lineage.get(dataset, (0, 0))
            for disposition in StagingDisposition:
                run_counts[disposition] += counts.get(disposition, 0)
            datasets.append(
                StagingDatasetReconciliation(
                    dataset=dataset,
                    target_model=target_model,
                    physical_dataset_id=physical_dataset_id,
                    role=role,
                    input_rows=input_rows,
                    input_rows_used=input_rows_used,
                    output_rows=output_rows,
                    lineage_links=lineage_links,
                    created_rows=max(output_rows - input_rows_used, 0),
                    combined_rows=max(lineage_links - output_rows, 0),
                    unrepresented_rows=input_rows - input_rows_used,
                    candidate_rows=counts.get(StagingDisposition.CANDIDATE, 0),
                    reference_rows=counts.get(StagingDisposition.REFERENCE, 0),
                    blocked_rows=counts.get(StagingDisposition.BLOCKED, 0),
                    quarantined_rows=counts.get(
                        StagingDisposition.QUARANTINED,
                        0,
                    ),
                    excluded_rows=counts.get(StagingDisposition.EXCLUDED, 0),
                )
            )
        total_rows = sum(run_counts.values())
        return (
            StagingReconciliation(
                total_rows=total_rows,
                candidate_rows=run_counts[StagingDisposition.CANDIDATE],
                reference_rows=run_counts[StagingDisposition.REFERENCE],
                blocked_rows=run_counts[StagingDisposition.BLOCKED],
                quarantined_rows=run_counts[StagingDisposition.QUARANTINED],
                excluded_rows=run_counts[StagingDisposition.EXCLUDED],
            ),
            tuple(datasets),
        )

    def _hash_direct_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        bindings: PreparationSessionBindings,
        datasets: tuple[StagingDatasetReconciliation, ...],
        issues: tuple[CanonicalIssue, ...],
        reconciliation: StagingReconciliation,
        control_totals: tuple[CanonicalControlTotal, ...],
    ) -> tuple[str, int]:
        """Validate and hash pending rows without copying their JSON payloads."""

        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("compiled_plan_hash", bindings.compiled_plan_hash)
        hasher.add_value("contract_version", bindings.contract_version)
        hasher.add_value(
            "control_totals",
            [item.to_portable_dict() for item in control_totals],
        )
        hasher.add_value(
            "datasets",
            [item.to_portable_dict() for item in datasets],
        )
        hasher.add_value("derived_plan_hash", bindings.derived_plan_hash)
        hasher.add_value("evaluator_version", bindings.evaluator_version)
        hasher.add_value(
            "issues",
            [item.to_portable_dict() for item in issues],
        )
        hasher.add_value("mapping_hash", bindings.mapping_hash)
        hasher.add_value("mapping_id", bindings.mapping_id)
        hasher.add_value(
            "physical_selection_hash",
            bindings.physical_selection_hash,
        )
        hasher.add_value(
            "reconciliation",
            reconciliation.to_portable_dict(),
        )
        hasher.start_array("rows")
        expected_ordinal = 0
        for batch in self._iter_direct_encoded_batches(
            workspace_id,
            run_id,
            batch_size=STAGING_ROW_BATCH_SIZE,
        ):
            for (
                ordinal,
                row_id,
                dataset,
                source_row,
                target_model,
                disposition,
                row_text,
            ) in batch:
                if int(ordinal) != expected_ordinal:
                    raise WorkspaceError(
                        "Stored direct preparation rows are not contiguous"
                    )
                if (
                    not str(row_id)
                    or not str(dataset)
                    or int(source_row) < 1
                    or not str(target_model)
                    or str(disposition) not in _STAGING_DISPOSITIONS
                ):
                    raise WorkspaceError(
                        "Stored direct preparation row metadata is inconsistent"
                    )
                encoded = str(row_text)
                if not encoded:
                    raise WorkspaceError("Stored direct preparation row is invalid")
                hasher.add_encoded_array_item(encoded)
                expected_ordinal += 1
        hasher.end_array()
        hasher.add_value("schema_hash", bindings.schema_hash)
        hasher.add_value(
            "source_selection_hash",
            bindings.source_selection_hash,
        )
        hasher.add_value("workspace_id", workspace_id)
        return hasher.finish(), expected_ordinal

    def _iter_direct_encoded_batches(
        self,
        workspace_id: str,
        run_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        """Yield stored or prepared-backed canonical JSON in ordinal order."""

        if connection is None:
            database_path = (
                self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            )
            with self._connect(database_path) as owned_connection:
                self._ensure_workspace_database_schema(owned_connection)
                yield from self._iter_direct_encoded_batches(
                    workspace_id,
                    run_id,
                    batch_size=batch_size,
                    connection=owned_connection,
                )
            return
        canonical_run_id = self._session_id(run_id)
        raw_projections = connection.execute(
            """
            SELECT projection.ordinal_start, projection.row_count,
                   projection.projection_json, manifest.manifest_json
              FROM canonical_prepared_projection AS projection
              JOIN prepared_snapshot_manifest AS manifest
                ON manifest.content_hash = projection.prepared_snapshot_hash
               AND manifest.dataset_id = projection.dataset_id
             WHERE projection.run_id = ?
             ORDER BY projection.ordinal_start
            """,
            [canonical_run_id],
        ).fetchall()
        projections: list[
            tuple[int, int, PreparedCanonicalProjection, PreparedSnapshot]
        ] = []
        for ordinal_start, row_count, projection_text, manifest_text in raw_projections:
            try:
                projection = PreparedCanonicalProjection.from_portable_dict(
                    json.loads(str(projection_text))
                )
                snapshot = PreparedSnapshot.from_json(str(manifest_text))
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Prepared canonical projection is invalid"
                ) from error
            if (
                projection.ordinal_start != int(ordinal_start)
                or projection.row_count != int(row_count)
                or projection.dataset_id != snapshot.dataset_id
                or projection.row_count != snapshot.row_count
                or projection.program.content_hash
                != snapshot.transformation_program_hash
                or projection.program.mapping_content_hash != snapshot.mapping_hash
                or projection.program.schema_hash != snapshot.schema_hash
            ):
                raise WorkspaceError("Prepared canonical projection metadata changed")
            projections.append(
                (
                    int(ordinal_start),
                    int(row_count),
                    projection,
                    snapshot,
                )
            )

        next_ordinal = 0
        for ordinal_start, row_count, projection, snapshot in projections:
            if ordinal_start < next_ordinal:
                raise WorkspaceError("Prepared canonical projection ordinals overlap")
            yield from self._iter_stored_direct_encoded_batches(
                connection,
                canonical_run_id,
                start=next_ordinal,
                stop=ordinal_start,
                batch_size=batch_size,
            )
            projection_end = ordinal_start + row_count
            counts = connection.execute(
                """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE row_json = '')
                  FROM canonical_staging_row
                 WHERE run_id = ?
                   AND ordinal >= ? AND ordinal < ?
                """,
                [canonical_run_id, ordinal_start, projection_end],
            ).fetchone()
            if counts is None or int(counts[0]) != row_count:
                raise WorkspaceError("Prepared canonical row index is incomplete")
            empty_count = int(counts[1])
            if empty_count == 0:
                yield from self._iter_stored_direct_encoded_batches(
                    connection,
                    canonical_run_id,
                    start=ordinal_start,
                    stop=projection_end,
                    batch_size=batch_size,
                )
            elif empty_count == row_count:
                yield from self._iter_projected_dataset_encoded_batches(
                    workspace_id,
                    canonical_run_id,
                    projection,
                    snapshot,
                    batch_size=batch_size,
                    connection=connection,
                )
            else:
                raise WorkspaceError("Prepared canonical value storage is inconsistent")
            next_ordinal = projection_end
        yield from self._iter_stored_direct_encoded_batches(
            connection,
            canonical_run_id,
            start=next_ordinal,
            stop=None,
            batch_size=batch_size,
        )

    @staticmethod
    def _iter_stored_direct_encoded_batches(
        connection,
        run_id: str,
        *,
        start: int,
        stop: int | None,
        batch_size: int,
    ):
        next_ordinal = start
        while True:
            if stop is not None and next_ordinal >= stop:
                return
            limit = batch_size if stop is None else min(batch_size, stop - next_ordinal)
            batch = connection.execute(
                """
                SELECT ordinal, row_id, dataset, source_row, target_model,
                       disposition, row_json
                  FROM canonical_staging_row
                 WHERE run_id = ? AND ordinal >= ?
                   AND (? IS NULL OR ordinal < ?)
                 ORDER BY ordinal
                 LIMIT ?
                """,
                [run_id, next_ordinal, stop, stop, limit],
            ).fetchall()
            if not batch:
                return
            yield batch
            next_ordinal += len(batch)

    def _iter_projected_dataset_encoded_batches(
        self,
        workspace_id: str,
        run_id: str,
        projection: PreparedCanonicalProjection,
        snapshot: PreparedSnapshot,
        *,
        batch_size: int,
        connection,
    ):
        try:
            artifact_context = self._artifacts.materialize_prepared_snapshot(
                workspace_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            )
            with artifact_context as path:
                if projection.set_based_projection:
                    base_disposition = (
                        StagingDisposition.REFERENCE.value
                        if projection.mode == "reference"
                        else StagingDisposition.CANDIDATE.value
                    )
                    exception = connection.execute(
                        """
                        SELECT 1
                          FROM canonical_staging_row AS row
                          LEFT JOIN canonical_staging_row_issue AS issue
                            ON issue.run_id = row.run_id
                           AND issue.ordinal = row.ordinal
                         WHERE row.run_id = ?
                           AND row.ordinal >= ? AND row.ordinal < ?
                           AND (row.disposition <> ? OR issue.ordinal IS NOT NULL)
                         LIMIT 1
                        """,
                        [
                            run_id,
                            projection.ordinal_start,
                            projection.ordinal_start + projection.row_count,
                            base_disposition,
                        ],
                    ).fetchone()
                    if exception is None:
                        query = projected_encoded_rows_sql(
                            projection,
                        )
                        projection_connection = duckdb.connect(
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
                        )
                        try:
                            local_offset = 0
                            while local_offset < projection.row_count:
                                local_stop = min(
                                    local_offset + batch_size,
                                    projection.row_count,
                                )
                                start = projection.ordinal_start + local_offset
                                stop = projection.ordinal_start + local_stop
                                batch = projection_connection.execute(
                                    query,
                                    [str(path), local_offset, local_stop],
                                ).fetchall()
                                metadata = connection.execute(
                                    """
                                    SELECT ordinal, row_id, dataset, source_row,
                                           target_model, disposition
                                      FROM canonical_staging_row
                                     WHERE run_id = ?
                                       AND ordinal >= ? AND ordinal < ?
                                     ORDER BY ordinal
                                    """,
                                    [run_id, start, stop],
                                ).fetchall()
                                if len(batch) != local_stop - local_offset or tuple(
                                    row[:6] for row in batch
                                ) != tuple(metadata):
                                    raise WorkspaceError(
                                        "Prepared canonical row index changed"
                                    )
                                yield batch
                                local_offset = local_stop
                            return
                        finally:
                            projection_connection.close()

                local_offset = 0
                for native_batch in iter_polars_prepared_batches(
                    path,
                    snapshot,
                    None,
                    projection.program,
                    batch_size=batch_size,
                    materialize_records=False,
                    collect_impacts=False,
                ):
                    start = projection.ordinal_start + local_offset
                    stop = start + len(native_batch.source_rows)
                    metadata = connection.execute(
                        """
                        SELECT ordinal, row_id, dataset, source_row,
                               target_model, disposition
                          FROM canonical_staging_row
                         WHERE run_id = ?
                           AND ordinal >= ? AND ordinal < ?
                         ORDER BY ordinal
                        """,
                        [run_id, start, stop],
                    ).fetchall()
                    if len(metadata) != len(native_batch.source_rows):
                        raise WorkspaceError(
                            "Prepared canonical row index is incomplete"
                        )
                    issue_rows = connection.execute(
                        """
                        SELECT ordinal, issue_ordinal, issue_json
                          FROM canonical_staging_row_issue
                         WHERE run_id = ?
                           AND ordinal >= ? AND ordinal < ?
                           AND issue_ordinal = 0
                         ORDER BY ordinal, issue_ordinal
                        """,
                        [run_id, start, stop],
                    ).fetchall()
                    overlays: dict[int, list[CanonicalIssue]] = {}
                    for ordinal, _issue_ordinal, issue_text in issue_rows:
                        try:
                            issue = CanonicalIssue.from_dict(
                                json.loads(str(issue_text))
                            )
                        except (TypeError, ValueError) as error:
                            raise WorkspaceError(
                                "Prepared canonical row issue is invalid"
                            ) from error
                        overlays.setdefault(int(ordinal), []).append(issue)
                    encoded_batch = []
                    values = zip(
                        metadata,
                        native_batch.source_rows,
                        native_batch.source_identities,
                        native_batch.target_identities,
                        native_batch.target_scopes,
                        native_batch.scalar_values,
                        native_batch.references,
                        native_batch.issues,
                        strict=True,
                    )
                    for (
                        metadata_row,
                        source_row,
                        source_identity,
                        target_identity,
                        target_scope,
                        scalar_values,
                        references,
                        issues,
                    ) in values:
                        ordinal = int(metadata_row[0])
                        projected = canonical_prepared_session_row(
                            dataset=projection.dataset,
                            source_row=source_row,
                            target_model=projection.program.target_model,
                            source_identity=source_identity,
                            target_identity=target_identity,
                            target_scope=target_scope,
                            scalar_values=scalar_values,
                            references=references,
                            issues=issues,
                            ordinal=ordinal,
                            mode=projection.mode,
                            source_hash=projection.source_hash,
                            source_selection_hash=(
                                projection.program.source_selection_hash
                            ),
                            mapping_hash=(projection.program.mapping_content_hash),
                            schema_hash=projection.program.schema_hash,
                            field_sources=projection.field_sources,
                            physical_dataset_id=(projection.physical_dataset_id),
                        )
                        try:
                            row = CanonicalRow.from_dict(json.loads(projected.row_json))
                        except (TypeError, ValueError) as error:
                            raise WorkspaceError(
                                "Projected canonical row is invalid"
                            ) from error
                        overlay = tuple(overlays.get(ordinal, ()))
                        stored_disposition = StagingDisposition(str(metadata_row[5]))
                        if overlay or row.disposition is not stored_disposition:
                            row = replace(
                                row,
                                disposition=stored_disposition,
                                issues=(*row.issues, *overlay),
                            )
                        if (
                            row.row_id != str(metadata_row[1])
                            or row.dataset != str(metadata_row[2])
                            or row.source_row != int(metadata_row[3])
                            or row.target_model != str(metadata_row[4])
                        ):
                            raise WorkspaceError("Prepared canonical row index changed")
                        encoded_batch.append(
                            (
                                ordinal,
                                row.row_id,
                                row.dataset,
                                row.source_row,
                                row.target_model,
                                row.disposition.value,
                                _canonical_json(row.to_portable_dict()),
                            )
                        )
                    yield encoded_batch
                    local_offset += len(native_batch.source_rows)
                if local_offset != projection.row_count:
                    raise WorkspaceError(
                        "Prepared canonical projection row count changed"
                    )
        except ArtifactStoreError as error:
            raise WorkspaceError(
                "Prepared canonical value artifact could not be verified"
            ) from error

    def _direct_rows_by_ordinal(
        self,
        workspace_id: str,
        run_id: str,
        ordinals: Sequence[int],
    ) -> dict[int, CanonicalRow]:
        wanted = {int(item) for item in ordinals}
        if not wanted:
            return {}
        rows: dict[int, CanonicalRow] = {}
        maximum = max(wanted)
        for batch in self._iter_direct_encoded_batches(
            workspace_id,
            run_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                observed = int(ordinal)
                if observed in wanted:
                    try:
                        rows[observed] = CanonicalRow.from_dict(
                            json.loads(str(row_text))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored direct preparation row is invalid"
                        ) from error
                if observed >= maximum:
                    return rows
        return rows

    def _load_canonical_row_range(
        self,
        workspace_id: str,
        session_id: str,
        start: int,
        stop: int,
    ) -> tuple[CanonicalRow, ...]:
        if stop <= start:
            return ()
        rows: list[CanonicalRow] = []
        for batch in self._iter_direct_encoded_batches(
            workspace_id,
            session_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                observed = int(ordinal)
                if observed >= stop:
                    return tuple(rows)
                if observed >= start:
                    rows.append(self._canonical_row(str(row_text)))
        return tuple(rows)

    def _iter_canonical_rows(
        self,
        workspace_id: str,
        session_id: str,
    ) -> Iterator[CanonicalRow]:
        next_ordinal = 0
        for batch in self._iter_direct_encoded_batches(
            workspace_id,
            session_id,
            batch_size=PREPARATION_SESSION_ROW_BATCH_SIZE,
        ):
            for ordinal, *_metadata, row_text in batch:
                if int(ordinal) != next_ordinal:
                    raise WorkspaceError(
                        "Stored preparation canonical rows are not contiguous"
                    )
                next_ordinal += 1
                yield self._canonical_row(str(row_text))

    @staticmethod
    def _canonical_row(row_text: str) -> CanonicalRow:
        try:
            return CanonicalRow.from_dict(json.loads(row_text))
        except (TypeError, ValueError) as error:
            raise WorkspaceError(
                "Stored preparation canonical row is invalid"
            ) from error

    @staticmethod
    def _require_derived_artifact_inputs(
        connection,
        session_id: str,
        artifact: DerivedValueArtifact,
    ) -> None:
        """Require every declared carrier through one set-based evidence query."""

        dataset_ids = [item.dataset_id for item in artifact.input_evidence]
        evidence_hashes = [item.evidence_hash for item in artifact.input_evidence]
        missing = connection.execute(
            """
            WITH expected AS (
                SELECT unnest(?) AS dataset_id,
                       unnest(?) AS evidence_hash
            )
            SELECT expected.dataset_id, expected.evidence_hash
              FROM expected
             WHERE NOT EXISTS (
                       SELECT 1
                         FROM source_snapshot_current AS current_source
                         JOIN source_snapshot_manifest AS source_manifest
                           ON source_manifest.content_hash =
                              current_source.content_hash
                          AND source_manifest.dataset_id =
                              current_source.dataset_id
                        WHERE current_source.dataset_id = expected.dataset_id
                          AND source_manifest.content_hash =
                              expected.evidence_hash
                   )
               AND NOT EXISTS (
                       SELECT 1
                         FROM preparation_session_snapshot AS prepared_binding
                         JOIN prepared_snapshot_manifest AS prepared_manifest
                           ON prepared_manifest.content_hash =
                              prepared_binding.content_hash
                          AND prepared_manifest.dataset_id =
                              prepared_binding.dataset_id
                        WHERE prepared_binding.session_id = ?
                          AND prepared_binding.dataset_id = expected.dataset_id
                          AND prepared_manifest.content_hash =
                              expected.evidence_hash
                   )
               AND NOT EXISTS (
                       SELECT 1
                         FROM preparation_session_derived_artifact AS derived_binding
                         JOIN derived_value_artifact_manifest AS derived_manifest
                           ON derived_manifest.content_hash =
                              derived_binding.content_hash
                          AND derived_manifest.dataset_id =
                              derived_binding.dataset_id
                        WHERE derived_binding.session_id = ?
                          AND derived_binding.dataset_id = expected.dataset_id
                          AND derived_manifest.content_hash =
                              expected.evidence_hash
                   )
             ORDER BY expected.dataset_id
            """,
            [dataset_ids, evidence_hashes, session_id, session_id],
        ).fetchall()
        if missing:
            raise WorkspaceError("Derived-value artifact input evidence is unavailable")

    @staticmethod
    def _derived_artifacts_from_bindings(
        rows: Sequence[tuple[object, object, object | None]],
    ) -> tuple[DerivedValueArtifact, ...]:
        artifacts: list[DerivedValueArtifact] = []
        for dataset_id, content_hash, manifest_json in rows:
            if manifest_json is None:
                raise WorkspaceError("Bound derived-value artifact manifest is missing")
            artifact = DerivedValueArtifact.from_json(str(manifest_json))
            if artifact.dataset_id != str(dataset_id) or artifact.content_hash != str(
                content_hash
            ):
                raise WorkspaceError(
                    "Bound derived-value artifact manifest is inconsistent"
                )
            artifacts.append(artifact)
        return tuple(artifacts)

    @staticmethod
    def _require_status(
        connection,
        session_id: str,
        expected: PreparationSessionStatus,
    ):
        row = connection.execute(
            """
            SELECT status, staged_row_count, impact_row_count
              FROM preparation_session
             WHERE session_id = ?
            """,
            [canonical_preparation_session_id(session_id)],
        ).fetchone()
        if row is None:
            raise WorkspaceError("Preparation session was not found")
        if PreparationSessionStatus(str(row[0])) is not expected:
            raise WorkspaceError("Preparation session is in the wrong state")
        return row
