"""DuckDB staging repository implementation."""

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
from ...projects import ProjectNotFoundError
from ...staging import StagingRunStatus, StagingRunSummary
from ...staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    CanonicalRow,
    CanonicalStagingRun,
)
from ...workspace_contracts import SourceSelection
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository





from .serialization import _canonical_json


class StagingRepository(DuckDbRepository):
    """Persistence operations for staging repository."""

    def publish_canonical_staging(
        self,
        project_id: str,
        run: CanonicalStagingRun,
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
        run_payload = run.to_portable_dict()
        run_content_hash = str(run_payload["content_hash"])
        try:
            CanonicalStagingRun.from_json(_canonical_json(run_payload))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Prepared data evidence is invalid") from error
        finally:
            del run_payload
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        published_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
                self._insert_canonical_rows(connection, run_id, run.rows)
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
    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
    ) -> CanonicalStagingRun | None:
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Prepared-data run identifier is invalid") from error
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
            rows = connection.execute(
                """
                SELECT row_json
                  FROM canonical_staging_row
                 WHERE run_id = ?
                 ORDER BY ordinal
                """,
                [canonical_run_id],
            ).fetchall()
        payload = {
            "content_hash": str(header[0]),
            "mapping_id": str(header[1]),
            "project_id": project_id,
            "physical_selection_hash": str(header[2]),
            "source_selection_hash": str(header[3]),
            "mapping_hash": str(header[4]),
            "schema_hash": str(header[5]),
            "derived_plan_hash": str(header[6]) if header[6] else None,
            "compiled_plan_hash": str(header[7]) if header[7] else None,
            "contract_version": int(header[8]),
            "evaluator_version": int(header[9]),
            "issues": json.loads(str(header[10])),
            "reconciliation": json.loads(str(header[11])),
            "datasets": json.loads(str(header[12])),
            "control_totals": json.loads(str(header[13])),
            "rows": [json.loads(str(item[0])) for item in rows],
        }
        try:
            return CanonicalStagingRun.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared-data evidence is invalid") from error
    @staticmethod
    def _insert_canonical_rows(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        rows: Sequence[CanonicalRow],
    ) -> None:
        for start in range(0, len(rows), STAGING_ROW_BATCH_SIZE):
            batch = rows[start : start + STAGING_ROW_BATCH_SIZE]
            values = [
                [
                    run_id,
                    start + offset,
                    row.row_id,
                    row.dataset,
                    row.source_row,
                    row.target_model,
                    row.disposition.value,
                    _canonical_json(row.to_portable_dict()),
                ]
                for offset, row in enumerate(batch)
            ]
            connection.execute(
                """
                INSERT INTO canonical_staging_row (
                    run_id, ordinal, row_id, dataset, source_row,
                    target_model, disposition, row_json
                )
                SELECT
                    json_extract_string(value, '$[0]'),
                    CAST(json_extract(value, '$[1]') AS BIGINT),
                    json_extract_string(value, '$[2]'),
                    json_extract_string(value, '$[3]'),
                    CAST(json_extract(value, '$[4]') AS BIGINT),
                    json_extract_string(value, '$[5]'),
                    json_extract_string(value, '$[6]'),
                    json_extract_string(value, '$[7]')
                  FROM json_each(?)
                """,
                [_canonical_json(values)],
            )
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
