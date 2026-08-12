"""DuckDB persistence for immutable Stage-E canonical staging evidence.

Publication verifies the submitted mapping, frozen physical/effective sources,
and derived plan in one transaction. A changed run advances the current pointer
and invalidates quality and every later artifact; identical content is reused.
"""

from __future__ import annotations

from .constants import STAGING_ROW_BATCH_SIZE

from datetime import (
    datetime,
    timezone,
)
import json
from typing import Sequence
from uuid import UUID, uuid4

import duckdb

from ...access import Actor
from ...artifacts import ArtifactStore, LocalArtifactStore
from ...projects import ProjectNotFoundError
from ...staging import StagingRunStatus, StagingRunSummary
from ...staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    CanonicalStagingRun,
    StagingDatasetReconciliation,
    StagingReconciliation,
    validate_canonical_row_bindings,
)
from ...workspace_contracts import SourceSelection
from ...workspace_errors import WorkspaceError
from ...domain.serialization import CanonicalJsonObjectHasher
from ...domain.staging.preparation_session import StoredCanonicalStagingRun
from .preparation_session_repository import PreparationSessionRepository
from .repository import DuckDbRepository





from .serialization import _canonical_json, _columnar_parameters


class StagingRepository(DuckDbRepository):
    """Implement canonical publication, batched row storage, and reassembly."""

    def __init__(
        self,
        database,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        super().__init__(database)
        self._artifacts = artifacts or LocalArtifactStore(database.root)

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun | StoredCanonicalStagingRun,
        *,
        mapping_version: int,
        actor: Actor,
    ) -> StagingRunSummary:
        """Atomically publish immutable canonical rows for one submitted mapping."""

        if run.project_id != project_id:
            raise WorkspaceError("Prepared data belongs to another project")
        if (
            run.contract_version != STAGING_CONTRACT_VERSION
            or run.evaluator_version != BROWSER_EVALUATOR_VERSION
        ):
            raise WorkspaceError(
                "Prepared data must be regenerated with the current evaluator"
            )
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        published_at = datetime.now(timezone.utc)
        pending_run_id = getattr(run.rows, "canonical_run_id", None)
        if pending_run_id is None:
            run_id = str(uuid4())
        else:
            try:
                run_id = str(UUID(str(pending_run_id)))
            except (ValueError, AttributeError) as error:
                raise WorkspaceError(
                    "Pending prepared-data run identifier is invalid"
                ) from error
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                mapping = connection.execute(
                    """
                    SELECT revision.mapping_id, revision.version,
                           revision.content_hash,
                           revision.source_selection_hash,
                           revision.schema_hash
                      FROM mapping_current AS current
                      JOIN mapping_revision AS revision
                        ON revision.mapping_id = current.mapping_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                       AND EXISTS (
                           SELECT 1
                             FROM mapping_submission AS submission
                            WHERE submission.mapping_id = revision.mapping_id
                              AND submission.version = revision.version
                              AND submission.content_hash = revision.content_hash
                       )
                    """
                ).fetchone()
                if mapping is None:
                    raise WorkspaceError(
                        "Submit the current field matches before saving prepared data"
                    )
                if (
                    str(mapping[0]) != run.mapping_id
                    or int(mapping[1]) != mapping_version
                    or str(mapping[2]) != run.mapping_hash
                    or str(mapping[3]) != run.source_selection_hash
                    or str(mapping[4]) != run.schema_hash
                ):
                    raise WorkspaceError(
                        "Prepared data no longer matches the submitted field matches"
                    )
                selection = connection.execute(
                    """
                    SELECT selection_json
                      FROM source_selection
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if selection is None:
                    raise WorkspaceError(
                        "Freeze the source datasets before saving prepared data"
                    )
                physical = SourceSelection.from_json(str(selection[0]))
                if physical.content_hash != run.physical_selection_hash:
                    raise WorkspaceError(
                        "Prepared data no longer matches the frozen source datasets"
                    )
                plan = connection.execute(
                    """
                    SELECT revision.content_hash
                      FROM derived_entity_plan_current AS current
                      JOIN derived_entity_plan_revision AS revision
                        ON revision.plan_id = current.plan_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                current_plan_hash = str(plan[0]) if plan else None
                if current_plan_hash != run.derived_plan_hash:
                    raise WorkspaceError(
                        "Prepared data no longer matches its related-record plan"
                    )

                current = connection.execute(
                    """
                    SELECT run.run_id, run.content_hash, run.mapping_id,
                           run.mapping_version, run.contract_version,
                           run.evaluator_version, run.status, run.published_at,
                           run.published_by, run.reconciliation_json,
                           run.dataset_reconciliation_json,
                           run.control_totals_json
                      FROM canonical_staging_current AS active
                      JOIN canonical_staging_run AS run
                        ON run.run_id = active.run_id
                     WHERE active.singleton_id = 1
                    """
                ).fetchone()
                run_content_hash = (
                    self._pending_canonical_content_hash(
                        connection,
                        run_id,
                        run,
                        mapping_version=mapping_version,
                    )
                    if pending_run_id is not None
                    else self._insert_canonical_rows(
                        connection,
                        run_id,
                        run,
                    )
                )
                if (
                    current is not None
                    and str(current[1]) == run_content_hash
                    and str(current[2]) == run.mapping_id
                    and int(current[3]) == mapping_version
                ):
                    connection.rollback()
                    return self._staging_summary(project_id, current)

                self._invalidate_quality(
                    connection,
                    reason="CANONICAL_STAGING_CHANGED",
                )

                if pending_run_id is not None:
                    promoted = connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = ?, published_at = ?, published_by = ?
                         WHERE run_id = ? AND status = ?
                        RETURNING run_id
                        """,
                        [
                            StagingRunStatus.PUBLISHED.value,
                            published_at.isoformat(),
                            actor.identity.display_name,
                            run_id,
                            StagingRunStatus.PENDING.value,
                        ],
                    ).fetchone()
                    if promoted is None:
                        raise WorkspaceError(
                            "Pending prepared data could not be published"
                        )
                else:
                    connection.execute(
                        """
                    INSERT INTO canonical_staging_run (
                        run_id, content_hash, mapping_id, mapping_version,
                        physical_selection_hash, source_selection_hash,
                        mapping_hash, schema_hash, derived_plan_hash,
                        compiled_plan_hash, contract_version,
                        evaluator_version, status,
                        published_at, published_by, row_count,
                        run_issues_json, reconciliation_json,
                        dataset_reconciliation_json, control_totals_json,
                        retired_at,
                        retired_reason, successor_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              NULL, NULL, NULL)
                        """,
                        [
                        run_id,
                        run_content_hash,
                        run.mapping_id,
                        mapping_version,
                        run.physical_selection_hash,
                        run.source_selection_hash,
                        run.mapping_hash,
                        run.schema_hash,
                        run.derived_plan_hash,
                        run.compiled_plan_hash,
                        run.contract_version,
                        run.evaluator_version,
                        StagingRunStatus.PUBLISHED.value,
                        published_at.isoformat(),
                        actor.identity.display_name,
                        len(run.rows),
                        _canonical_json(
                            [item.to_portable_dict() for item in run.issues]
                        ),
                        _canonical_json(run.reconciliation.to_portable_dict()),
                        _canonical_json(
                            [item.to_portable_dict() for item in run.datasets]
                        ),
                        _canonical_json(
                            [
                                item.to_portable_dict()
                                for item in run.control_totals
                            ]
                        ),
                        ],
                    )
                stored_count = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM canonical_staging_row
                     WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchone()
                if stored_count is None or int(stored_count[0]) != len(run.rows):
                    raise WorkspaceError("Prepared rows were not stored completely")
                if current is not None:
                    connection.execute(
                        """
                        UPDATE canonical_staging_run
                           SET status = ?, retired_at = ?, retired_reason = ?,
                               successor_run_id = ?
                         WHERE run_id = ?
                        """,
                        [
                            StagingRunStatus.SUPERSEDED.value,
                            published_at.isoformat(),
                            "NEW_CANONICAL_RUN",
                            run_id,
                            str(current[0]),
                        ],
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO canonical_staging_current
                    VALUES (1, ?)
                    """,
                    [run_id],
                )
                connection.execute(
                    """
                    UPDATE project
                       SET current_run_id = NULL,
                           approval_status = 'INVALIDATED'
                    """
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="CANONICAL_STAGING_PUBLISHED",
                    detail=(
                        f"run {run_id}: {len(run.rows)} prepared row(s); "
                        f"{run_content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return StagingRunSummary(
            run_id=run_id,
            project_id=project_id,
            content_hash=run_content_hash,
            mapping_id=run.mapping_id,
            mapping_version=mapping_version,
            contract_version=run.contract_version,
            evaluator_version=run.evaluator_version,
            status=StagingRunStatus.PUBLISHED,
            published_at=published_at,
            published_by=actor.identity.display_name,
            reconciliation=run.reconciliation,
            datasets=run.datasets,
            control_totals=run.control_totals,
        )

    @staticmethod
    def _pending_canonical_content_hash(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        run: StoredCanonicalStagingRun,
        *,
        mapping_version: int,
    ) -> str:
        """Validate a finalized pending header without reinserting row JSON."""

        header = connection.execute(
            """
            SELECT content_hash, mapping_id, mapping_version,
                   physical_selection_hash, source_selection_hash,
                   mapping_hash, schema_hash, derived_plan_hash,
                   compiled_plan_hash, contract_version, evaluator_version,
                   status, row_count, run_issues_json,
                   reconciliation_json, dataset_reconciliation_json,
                   control_totals_json
              FROM canonical_staging_run
             WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()
        if header is None or str(header[11]) != StagingRunStatus.PENDING.value:
            raise WorkspaceError("Direct prepared data is not pending")
        expected = (
            run.mapping_id,
            mapping_version,
            run.physical_selection_hash,
            run.source_selection_hash,
            run.mapping_hash,
            run.schema_hash,
            run.derived_plan_hash,
            run.compiled_plan_hash,
            run.contract_version,
            run.evaluator_version,
            len(run.rows),
            _canonical_json(
                [item.to_portable_dict() for item in run.issues]
            ),
            _canonical_json(run.reconciliation.to_portable_dict()),
            _canonical_json(
                [item.to_portable_dict() for item in run.datasets]
            ),
            _canonical_json(
                [item.to_portable_dict() for item in run.control_totals]
            ),
        )
        actual = (
            str(header[1]),
            int(header[2]),
            str(header[3]),
            str(header[4]),
            str(header[5]),
            str(header[6]),
            str(header[7]) if header[7] else None,
            str(header[8]),
            int(header[9]),
            int(header[10]),
            int(header[12]),
            str(header[13]),
            str(header[14]),
            str(header[15]),
            str(header[16]),
        )
        if actual != expected:
            raise WorkspaceError(
                "Pending prepared-data evidence is inconsistent"
            )
        stored = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT ordinal),
                   MIN(ordinal), MAX(ordinal)
              FROM canonical_staging_row
             WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()
        row_count = len(run.rows)
        expected_bounds = (
            row_count,
            row_count,
            0 if row_count else None,
            row_count - 1 if row_count else None,
        )
        if stored is None or tuple(stored) != expected_bounds:
            raise WorkspaceError("Pending prepared rows are incomplete")
        content_hash = str(header[0])
        if not content_hash.startswith("sha256:") or len(content_hash) != 71:
            raise WorkspaceError("Pending prepared-data hash is invalid")
        if run.validated_content_hash != content_hash:
            raise WorkspaceError(
                "Pending prepared-data hash changed unexpectedly"
            )
        return content_hash
    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None:
        """Return the summary selected by the current published-run pointer."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT run.run_id, run.content_hash, run.mapping_id,
                       run.mapping_version, run.contract_version,
                       run.evaluator_version, run.status, run.published_at,
                       run.published_by, run.reconciliation_json,
                       run.dataset_reconciliation_json,
                       run.control_totals_json
                  FROM canonical_staging_current AS active
                  JOIN canonical_staging_run AS run
                    ON run.run_id = active.run_id
                 WHERE active.singleton_id = 1
                   AND run.status = 'PUBLISHED'
                """
            ).fetchone()
        return self._staging_summary(project_id, row) if row else None
    def get_canonical_staging_run(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None:
        """Reassemble a full run and verify both stored and expected hashes."""

        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Prepared-data run identifier is invalid") from error
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            header = connection.execute(
                """
                SELECT content_hash, mapping_id, physical_selection_hash,
                       source_selection_hash, mapping_hash, schema_hash,
                       derived_plan_hash, compiled_plan_hash, contract_version,
                       evaluator_version, run_issues_json, reconciliation_json,
                       dataset_reconciliation_json, control_totals_json
                  FROM canonical_staging_run
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if header is None:
                return None
            if (
                expected_content_hash is not None
                and str(header[0]) != expected_content_hash
            ):
                raise WorkspaceError(
                    "Published prepared-data evidence changed unexpectedly"
                )
            try:
                stored_content_hash = str(header[0])
                contract_version = int(header[8])
                evaluator_version = int(header[9])
                issues_payload = json.loads(str(header[10]))
                reconciliation_payload = json.loads(str(header[11]))
                datasets_payload = json.loads(str(header[12]))
                control_totals_payload = json.loads(str(header[13]))
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Stored prepared-data evidence is invalid"
                ) from error

            hasher = CanonicalJsonObjectHasher()
            hasher.add_value(
                "compiled_plan_hash",
                str(header[7]) if header[7] else None,
            )
            hasher.add_value("contract_version", contract_version)
            hasher.add_value("control_totals", control_totals_payload)
            hasher.add_value("datasets", datasets_payload)
            hasher.add_value(
                "derived_plan_hash",
                str(header[6]) if header[6] else None,
            )
            hasher.add_value("evaluator_version", evaluator_version)
            hasher.add_value("issues", issues_payload)
            hasher.add_value("mapping_hash", str(header[4]))
            hasher.add_value("mapping_id", str(header[1]))
            hasher.add_value("physical_selection_hash", str(header[2]))
            hasher.add_value("project_id", project_id)
            hasher.add_value("reconciliation", reconciliation_payload)
            hasher.start_array("rows")

            rows: list[CanonicalRow] = []
            expected_ordinal = 0
            prepared_backed = connection.execute(
                """
                SELECT 1
                  FROM canonical_staging_row
                 WHERE run_id = ? AND row_json = ''
                 LIMIT 1
                """,
                [canonical_run_id],
            ).fetchone()
            if prepared_backed is not None:
                encoded_batches = PreparationSessionRepository(
                    self._database,
                    self._artifacts,
                )._iter_direct_encoded_batches(
                    project_id,
                    canonical_run_id,
                    batch_size=STAGING_ROW_BATCH_SIZE,
                    connection=connection,
                )
            else:
                cursor = connection.execute(
                    """
                    SELECT ordinal, row_id, dataset, source_row,
                           target_model, disposition, row_json
                      FROM canonical_staging_row
                     WHERE run_id = ?
                     ORDER BY ordinal
                    """,
                    [canonical_run_id],
                )

                def stored_batches():
                    while batch := cursor.fetchmany(STAGING_ROW_BATCH_SIZE):
                        yield batch

                encoded_batches = stored_batches()
            for batch in encoded_batches:
                for ordinal, *_metadata, row_text in batch:
                    if int(ordinal) != expected_ordinal:
                        raise WorkspaceError(
                            "Stored prepared-data row ordering is invalid"
                        )
                    expected_ordinal += 1
                    encoded_row = str(row_text)
                    hasher.add_encoded_array_item(encoded_row)
                    try:
                        rows.append(
                            CanonicalRow.from_dict(json.loads(encoded_row))
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored prepared-data row evidence is invalid"
                        ) from error
            hasher.end_array()
            hasher.add_value("schema_hash", str(header[5]))
            hasher.add_value("source_selection_hash", str(header[3]))
            if hasher.finish() != stored_content_hash:
                raise WorkspaceError("Stored prepared-data content hash is invalid")

        try:
            return CanonicalStagingRun(
                project_id=project_id,
                mapping_id=str(header[1]),
                physical_selection_hash=str(header[2]),
                source_selection_hash=str(header[3]),
                mapping_hash=str(header[4]),
                schema_hash=str(header[5]),
                derived_plan_hash=str(header[6]) if header[6] else None,
                compiled_plan_hash=str(header[7]) if header[7] else None,
                contract_version=contract_version,
                evaluator_version=evaluator_version,
                issues=tuple(
                    CanonicalIssue.from_dict(item) for item in issues_payload
                ),
                reconciliation=StagingReconciliation.from_dict(
                    reconciliation_payload
                ),
                datasets=tuple(
                    StagingDatasetReconciliation.from_dict(item)
                    for item in datasets_payload
                ),
                control_totals=tuple(
                    CanonicalControlTotal.from_dict(item)
                    for item in control_totals_payload
                ),
                rows=tuple(rows),
            )
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared-data evidence is invalid") from error
    @staticmethod
    def _insert_canonical_rows(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        run: CanonicalStagingRun | StoredCanonicalStagingRun,
    ) -> str:
        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("compiled_plan_hash", run.compiled_plan_hash)
        hasher.add_value("contract_version", run.contract_version)
        hasher.add_value(
            "control_totals",
            [item.to_portable_dict() for item in run.control_totals],
        )
        hasher.add_value(
            "datasets",
            [item.to_portable_dict() for item in run.datasets],
        )
        hasher.add_value("derived_plan_hash", run.derived_plan_hash)
        hasher.add_value("evaluator_version", run.evaluator_version)
        hasher.add_value(
            "issues",
            [item.to_portable_dict() for item in run.issues],
        )
        hasher.add_value("mapping_hash", run.mapping_hash)
        hasher.add_value("mapping_id", run.mapping_id)
        hasher.add_value("physical_selection_hash", run.physical_selection_hash)
        hasher.add_value("project_id", run.project_id)
        hasher.add_value("reconciliation", run.reconciliation.to_portable_dict())
        hasher.start_array("rows")
        encoded_batches = getattr(run.rows, "iter_encoded_batches", None)
        if callable(encoded_batches):
            expected_ordinal = 0
            for batch in encoded_batches(connection, STAGING_ROW_BATCH_SIZE):
                values: list[list[object]] = []
                for (
                    ordinal,
                    row_id,
                    dataset,
                    source_row,
                    target_model,
                    disposition,
                    row_json,
                ) in batch:
                    if int(ordinal) != expected_ordinal:
                        raise WorkspaceError(
                            "Stored preparation rows are not contiguous"
                        )
                    try:
                        row = CanonicalRow.from_dict(json.loads(str(row_json)))
                        validate_canonical_row_bindings(
                            row,
                            source_selection_hash=run.source_selection_hash,
                            mapping_hash=run.mapping_hash,
                            schema_hash=run.schema_hash,
                            derived_plan_hash=run.derived_plan_hash,
                        )
                    except (TypeError, ValueError) as error:
                        raise WorkspaceError(
                            "Stored preparation row is invalid"
                        ) from error
                    if (
                        row.row_id != str(row_id)
                        or row.dataset != str(dataset)
                        or row.source_row != int(source_row)
                        or row.target_model != str(target_model)
                        or row.disposition.value != str(disposition)
                    ):
                        raise WorkspaceError(
                            "Stored preparation row metadata is inconsistent"
                        )
                    encoded = str(row_json)
                    hasher.add_encoded_array_item(encoded)
                    values.append(
                        [
                            run_id,
                            expected_ordinal,
                            row.row_id,
                            row.dataset,
                            row.source_row,
                            row.target_model,
                            row.disposition.value,
                            encoded,
                        ]
                    )
                    expected_ordinal += 1
                connection.execute(
                    """
                    INSERT INTO canonical_staging_row (
                        run_id, ordinal, row_id, dataset, source_row,
                        target_model, disposition, row_json
                    )
                    SELECT
                        unnest(?), unnest(?), unnest(?), unnest(?),
                        unnest(?), unnest(?), unnest(?), unnest(?)
                    """,
                    _columnar_parameters(values),
                )
            if expected_ordinal != len(run.rows):
                raise WorkspaceError("Stored preparation rows are incomplete")
            hasher.end_array()
            hasher.add_value("schema_hash", run.schema_hash)
            hasher.add_value("source_selection_hash", run.source_selection_hash)
            return hasher.finish()
        for start in range(0, len(run.rows), STAGING_ROW_BATCH_SIZE):
            batch = run.rows[start : start + STAGING_ROW_BATCH_SIZE]
            values: list[list[object]] = []
            for offset, row in enumerate(batch):
                row_json = _canonical_json(row.to_portable_dict())
                hasher.add_encoded_array_item(row_json)
                values.append([
                    run_id,
                    start + offset,
                    row.row_id,
                    row.dataset,
                    row.source_row,
                    row.target_model,
                    row.disposition.value,
                    row_json,
                ])
            connection.execute(
                """
                INSERT INTO canonical_staging_row (
                    run_id, ordinal, row_id, dataset, source_row,
                    target_model, disposition, row_json
                )
                SELECT
                    unnest(?), unnest(?), unnest(?), unnest(?),
                    unnest(?), unnest(?), unnest(?), unnest(?)
                """,
                _columnar_parameters(values),
            )
        hasher.end_array()
        hasher.add_value("schema_hash", run.schema_hash)
        hasher.add_value("source_selection_hash", run.source_selection_hash)
        return hasher.finish()
    @staticmethod
    def _staging_summary(
        project_id: str,
        row: Sequence[object],
    ) -> StagingRunSummary:
        from ...staging_contracts import (
            CanonicalControlTotal,
            StagingDatasetReconciliation,
            StagingReconciliation,
        )

        return StagingRunSummary(
            run_id=str(row[0]),
            project_id=project_id,
            content_hash=str(row[1]),
            mapping_id=str(row[2]),
            mapping_version=int(row[3]),
            contract_version=int(row[4]),
            evaluator_version=int(row[5]),
            status=StagingRunStatus(str(row[6])),
            published_at=datetime.fromisoformat(str(row[7])),
            published_by=str(row[8]),
            reconciliation=StagingReconciliation.from_dict(
                json.loads(str(row[9]))
            ),
            datasets=tuple(
                StagingDatasetReconciliation.from_dict(item)
                for item in json.loads(str(row[10]))
            ),
            control_totals=tuple(
                CanonicalControlTotal.from_dict(item)
                for item in json.loads(str(row[11]))
            ),
        )
