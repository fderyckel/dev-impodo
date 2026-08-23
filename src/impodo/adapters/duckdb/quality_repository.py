"""DuckDB persistence for Stage-F rules, overlays, and quarantine evidence.

Publication verifies current staging/mapping inputs inside one transaction,
stores row evidence in batches, advances the current pointer, and invalidates
normalization/preflight evidence when quality semantics change.
"""

from __future__ import annotations

from .constants import (
    DUCKDB_JSON_BATCH_MAX_BYTES,
    QUALITY_ROW_BATCH_SIZE,
)

from datetime import (
    datetime,
    timezone,
)
import json
from typing import Sequence
from uuid import UUID, uuid4

import duckdb

from ...access import Actor
from ...workspace_state import WorkspaceStateNotFoundError
from ...quality import (
    QUALITY_CONTRACT_VERSION,
    QUALITY_EVALUATOR_VERSION,
    QUALITY_RULESET_CONTRACT_VERSION,
    QualityDisposition,
    QualityIssue,
    QualityOutcomePolicy,
    QualityRuleSet,
    QualityReviewItem,
    QualityReviewPage,
    QualityRowResult,
    QualityRun,
    QualityRunStatus,
    QualityRunSummary,
    QuarantineEntry,
    SourceAccountingState,
    StoredQualityRun,
    retention_context_hash,
)
from ...staging_contracts import CanonicalRow
from ...workspace_errors import WorkspaceError
from ...domain.serialization import CanonicalJsonObjectHasher
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository, ProjectAggregateReader





from .serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)


_QUALITY_ROW_RESULT_JSON_STRUCTURE = """[{
    "ordinal":"BIGINT",
    "row_id":"VARCHAR",
    "dataset":"VARCHAR",
    "source_row":"BIGINT",
    "record_label":"VARCHAR",
    "base_disposition":"VARCHAR",
    "effective_disposition":"VARCHAR",
    "requires_review":"BOOLEAN",
    "row_json":"VARCHAR"
}]"""

_SOURCE_ACCOUNTING_ENTRY_JSON_STRUCTURE = """[{
    "ordinal":"BIGINT",
    "physical_dataset_id":"VARCHAR",
    "source_row":"BIGINT",
    "state":"VARCHAR",
    "entry_json":"VARCHAR"
}]"""

_SOURCE_ACCOUNTING_LINK_JSON_STRUCTURE = """[{
    "accounting_ordinal":"BIGINT",
    "row_id":"VARCHAR"
}]"""


class QualityRepository(DuckDbRepository):
    """Implement the quality port with immutable revisions and current pointers."""

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        projects: ProjectAggregateReader,
    ) -> None:
        super().__init__(database)
        self._projects = projects

    def get_current_quality_ruleset(
        self,
        project_id: str,
    ) -> QualityRuleSet | None:
        """Load and hash-validate the ruleset selected as current."""

        value = self._read_singleton_json(
            project_id,
            """
            SELECT revision.ruleset_json
              FROM quality_ruleset_current AS current
              JOIN quality_ruleset_revision AS revision
                ON revision.ruleset_id = current.ruleset_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        if not value:
            return None
        try:
            return QualityRuleSet.from_json(value)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored data-check rules are invalid") from error
    def publish_quality_ruleset(
        self,
        project_id: str,
        ruleset: QualityRuleSet,
        *,
        actor: Actor,
    ) -> QualityRuleSet:
        """Publish one complete guided ruleset and retire its quality result."""

        if ruleset.project_id != project_id:
            raise WorkspaceError("Data-check rules belong to another project")
        if ruleset.contract_version != QUALITY_RULESET_CONTRACT_VERSION:
            raise WorkspaceError("Data-check rules use an unsupported version")
        try:
            QualityRuleSet.from_json(ruleset.to_json())
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Data-check rules are invalid") from error
        project = self._projects.get(project_id)
        if any(
            item.review_by_days is not None
            and item.review_by_days > project.retention_days
            for item in ruleset.rules
        ):
            raise WorkspaceError(
                "A data-check review date exceeds the project retention period"
            )
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        created_at = datetime.now(timezone.utc)
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                mapping = connection.execute(
                    """
                    SELECT revision.content_hash, revision.schema_hash
                      FROM mapping_current AS current
                      JOIN mapping_revision AS revision
                        ON revision.mapping_id = current.mapping_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                if mapping is None or (
                    str(mapping[0]) != ruleset.mapping_hash
                    or str(mapping[1]) != ruleset.schema_hash
                ):
                    raise WorkspaceError(
                        "Data checks no longer match the current field matches"
                    )
                current = connection.execute(
                    """
                    SELECT revision.ruleset_id, revision.version,
                           revision.content_hash, revision.ruleset_json
                      FROM quality_ruleset_current AS active
                      JOIN quality_ruleset_revision AS revision
                        ON revision.ruleset_id = active.ruleset_id
                       AND revision.version = active.version
                     WHERE active.singleton_id = 1
                    """
                ).fetchone()
                if current is not None and str(current[2]) == ruleset.content_hash:
                    connection.rollback()
                    return QualityRuleSet.from_json(str(current[3]))
                if current is None:
                    if ruleset.parent_version is not None:
                        raise WorkspaceError(
                            "The data-check parent version is no longer current"
                        )
                elif (
                    str(current[0]) != ruleset.ruleset_id
                    or int(current[1]) != ruleset.parent_version
                    or ruleset.version != int(current[1]) + 1
                ):
                    raise WorkspaceError(
                        "The data checks were changed by another request"
                    )
                connection.execute(
                    """
                    INSERT INTO quality_ruleset_revision (
                        ruleset_id, version, parent_version, mapping_hash,
                        schema_hash, content_hash, contract_version,
                        created_at, created_by, ruleset_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ruleset.ruleset_id,
                        ruleset.version,
                        ruleset.parent_version,
                        ruleset.mapping_hash,
                        ruleset.schema_hash,
                        ruleset.content_hash,
                        ruleset.contract_version,
                        created_at.isoformat(),
                        actor.identity.display_name,
                        ruleset.to_json(),
                    ],
                )
                self._invalidate_quality(
                    connection,
                    reason="QUALITY_RULESET_CHANGED",
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO quality_ruleset_current
                    VALUES (1, ?, ?)
                    """,
                    [ruleset.ruleset_id, ruleset.version],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="QUALITY_RULESET_PUBLISHED",
                    detail=(
                        f"version {ruleset.version}: "
                        f"{len(ruleset.rules)} data check(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return ruleset
    def publish_quality_run(
        self,
        project_id: str,
        run: QualityRun | StoredQualityRun,
        *,
        staging_run_id: str,
        effective_dataset_run_id: str | None = None,
        actor: Actor,
    ) -> QualityRunSummary:
        """Atomically publish a complete quality overlay and quarantine set."""

        if run.project_id != project_id:
            raise WorkspaceError("Quality evidence belongs to another project")
        if (
            run.contract_version != QUALITY_CONTRACT_VERSION
            or run.evaluator_version != QUALITY_EVALUATOR_VERSION
        ):
            raise WorkspaceError(
                "Quality evidence must be regenerated with the current evaluator"
            )
        project = self._projects.get(project_id)
        if run.retention_context_hash != retention_context_hash(project):
            raise WorkspaceError(
                "Quality evidence no longer matches project ownership and retention"
            )
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        published_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        summary_counts = _quality_summary_counts(run)
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                staging = connection.execute(
                    """
                    SELECT run.run_id, run.content_hash, run.mapping_hash,
                           run.schema_hash
                      FROM canonical_staging_current AS current
                      JOIN canonical_staging_run AS run
                        ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1
                       AND run.status = 'PUBLISHED'
                    """
                ).fetchone()
                if staging is None or (
                    str(staging[0]) != staging_run_id
                    or str(staging[1]) != run.staging_content_hash
                    or str(staging[2]) != run.mapping_hash
                    or str(staging[3]) != run.schema_hash
                ):
                    raise WorkspaceError(
                        "Quality evidence no longer matches current prepared data"
                    )
                if effective_dataset_run_id is None:
                    if run.effective_dataset_hash != run.staging_content_hash:
                        raise WorkspaceError(
                            "Quality evidence requires the current resolved dataset"
                        )
                else:
                    effective = connection.execute(
                        """
                        SELECT resolution.run_id,
                               resolution.effective_content_hash,
                               resolution.staging_content_hash
                          FROM effective_dataset_current AS current
                          JOIN resolution_run AS resolution
                            ON resolution.run_id = current.run_id
                         WHERE current.singleton_id = 1
                           AND resolution.status = 'FROZEN'
                        """
                    ).fetchone()
                    if effective is None or (
                        str(effective[0]) != effective_dataset_run_id
                        or str(effective[1]) != run.effective_dataset_hash
                        or str(effective[2]) != run.staging_content_hash
                    ):
                        raise WorkspaceError(
                            "Quality evidence no longer matches current resolved data"
                        )
                ruleset = connection.execute(
                    """
                    SELECT revision.content_hash
                      FROM quality_ruleset_current AS current
                      JOIN quality_ruleset_revision AS revision
                        ON revision.ruleset_id = current.ruleset_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                if ruleset is None or str(ruleset[0]) != run.ruleset_hash:
                    raise WorkspaceError(
                        "Quality evidence no longer matches current data checks"
                    )
                current = connection.execute(
                    """
                    SELECT run_id, content_hash, staging_run_id,
                           staging_content_hash, ruleset_hash, status,
                           published_at, published_by, summary_json,
                           effective_dataset_run_id, effective_dataset_hash
                      FROM quality_run
                     WHERE run_id = (
                         SELECT run_id FROM quality_current
                          WHERE singleton_id = 1
                     )
                    """
                ).fetchone()
                run_content_hash = self._insert_quality_evidence(
                    connection,
                    run_id,
                    run,
                )
                if current is not None and str(current[1]) == run_content_hash:
                    connection.rollback()
                    return self._quality_summary(project_id, current)
                self._invalidate_normalization(
                    connection,
                    reason="QUALITY_RUN_CHANGED",
                )
                connection.execute(
                    """
                    INSERT INTO quality_run (
                        run_id, content_hash, staging_run_id,
                        staging_content_hash, ruleset_hash, mapping_hash,
                        schema_hash, retention_context_hash, contract_version,
                        evaluator_version, status, published_at, published_by,
                        row_count, source_count, issue_count, quarantine_count,
                        summary_json, effective_dataset_run_id,
                        effective_dataset_hash, retired_at, retired_reason,
                        successor_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              NULL, NULL, NULL)
                    """,
                    [
                        run_id,
                        run_content_hash,
                        staging_run_id,
                        run.staging_content_hash,
                        run.ruleset_hash,
                        run.mapping_hash,
                        run.schema_hash,
                        run.retention_context_hash,
                        run.contract_version,
                        run.evaluator_version,
                        QualityRunStatus.PUBLISHED.value,
                        published_at.isoformat(),
                        actor.identity.display_name,
                        len(run.row_results),
                        len(run.source_accounting),
                        len(run.issues),
                        len(run.quarantine),
                        _canonical_json(summary_counts),
                        effective_dataset_run_id,
                        run.effective_dataset_hash,
                    ],
                )
                stored = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM quality_row_result WHERE run_id = ?),
                        (SELECT COUNT(*) FROM source_accounting_entry WHERE run_id = ?),
                        (SELECT COUNT(*) FROM quality_issue WHERE run_id = ?),
                        (SELECT COUNT(*) FROM quality_quarantine_entry WHERE run_id = ?)
                    """,
                    [run_id, run_id, run_id, run_id],
                ).fetchone()
                sparse_projection = connection.execute(
                    """
                    SELECT projection_json
                      FROM quality_evidence_projection
                     WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchone()
                if effective_dataset_run_id is None:
                    linked = connection.execute(
                        """
                        SELECT COUNT(*)
                          FROM quality_row_result AS quality
                          JOIN canonical_staging_row AS staging
                            ON staging.run_id = ?
                           AND staging.row_id = quality.row_id
                         WHERE quality.run_id = ?
                        """,
                        [staging_run_id, run_id],
                    ).fetchone()
                else:
                    linked = connection.execute(
                        """
                        SELECT COUNT(*)
                          FROM quality_row_result AS quality
                          JOIN effective_row AS effective
                            ON effective.run_id = ?
                           AND effective.row_id = quality.row_id
                         WHERE quality.run_id = ?
                        """,
                        [effective_dataset_run_id, run_id],
                    ).fetchone()
                stored_counts = (
                    tuple(int(item) for item in stored)
                    if stored is not None
                    else ()
                )
                if sparse_projection is None:
                    complete = (
                        stored_counts
                        == (
                            len(run.row_results),
                            len(run.source_accounting),
                            len(run.issues),
                            len(run.quarantine),
                        )
                        and linked is not None
                        and int(linked[0]) == len(run.row_results)
                    )
                else:
                    canonical_count = connection.execute(
                        """
                        SELECT COUNT(*) FROM canonical_staging_row
                         WHERE run_id = ?
                        """,
                        [staging_run_id],
                    ).fetchone()
                    complete = (
                        len(stored_counts) == 4
                        and stored_counts[0] <= len(run.row_results)
                        and stored_counts[1] == 0
                        and stored_counts[2:] == (
                            len(run.issues),
                            len(run.quarantine),
                        )
                        and linked is not None
                        and int(linked[0]) == stored_counts[0]
                        and canonical_count is not None
                        and int(canonical_count[0]) == len(run.row_results)
                        and len(run.source_accounting) == len(run.row_results)
                    )
                if not complete:
                    raise WorkspaceError("Quality evidence was not stored completely")
                if current is not None:
                    connection.execute(
                        """
                        UPDATE quality_run
                           SET status = ?, retired_at = ?, retired_reason = ?,
                               successor_run_id = ?
                         WHERE run_id = ?
                        """,
                        [
                            QualityRunStatus.SUPERSEDED.value,
                            published_at.isoformat(),
                            "NEW_QUALITY_RUN",
                            run_id,
                            str(current[0]),
                        ],
                    )
                    connection.execute(
                        """
                        UPDATE quality_quarantine_entry
                           SET superseded_by_run_id = ?
                         WHERE run_id = ?
                        """,
                        [run_id, str(current[0])],
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO quality_current VALUES (1, ?)",
                    [run_id],
                )
                connection.execute(
                    """
                    UPDATE workspace_state
                       SET current_run_id = NULL,
                           approval_status = 'INVALIDATED'
                    """
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="QUALITY_RUN_PUBLISHED",
                    detail=(
                        f"run {run_id}: {summary_counts['ready_count']} ready; "
                        f"{summary_counts['review_count']} review; "
                        f"{summary_counts['quarantined_count']} set aside; "
                        f"{summary_counts['blocked_count']} setup"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return QualityRunSummary(
            run_id=run_id,
            project_id=project_id,
            content_hash=run_content_hash,
            staging_run_id=staging_run_id,
            staging_content_hash=run.staging_content_hash,
            ruleset_hash=run.ruleset_hash,
            status=QualityRunStatus.PUBLISHED,
            published_at=published_at,
            published_by=actor.identity.display_name,
            ready_count=summary_counts["ready_count"],
            review_count=summary_counts["review_count"],
            quarantined_count=summary_counts["quarantined_count"],
            excluded_count=summary_counts["excluded_count"],
            blocked_count=summary_counts["blocked_count"],
            effective_dataset_run_id=effective_dataset_run_id,
            effective_dataset_hash=run.effective_dataset_hash,
        )
    def get_current_quality_summary(
        self,
        project_id: str,
    ) -> QualityRunSummary | None:
        """Return the current non-retired quality run's lifecycle projection."""

        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT run.run_id, run.content_hash, run.staging_run_id,
                       run.staging_content_hash, run.ruleset_hash, run.status,
                       run.published_at, run.published_by, run.summary_json,
                       run.effective_dataset_run_id,
                       run.effective_dataset_hash
                  FROM quality_current AS current
                  JOIN quality_run AS run ON run.run_id = current.run_id
                 WHERE current.singleton_id = 1
                   AND run.status = 'PUBLISHED'
                """
            ).fetchone()
        return self._quality_summary(project_id, row) if row else None
    def get_quality_run(
        self,
        project_id: str,
        run_id: str,
    ) -> QualityRun | None:
        """Reassemble and validate a complete quality run from row tables."""

        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Quality run identifier is invalid") from error
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            header = connection.execute(
                """
                SELECT content_hash, staging_run_id, staging_content_hash,
                       ruleset_hash, mapping_hash, schema_hash,
                       retention_context_hash, contract_version,
                       evaluator_version, effective_dataset_run_id,
                       effective_dataset_hash
                  FROM quality_run
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if header is None:
                return None
            projection_row = connection.execute(
                """
                SELECT projection_json FROM quality_evidence_projection
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            projection = (
                json.loads(str(projection_row[0]))
                if projection_row is not None
                else {}
            )
            sparse_rows = projection.get("row_results") == "direct-defaults-v1"
            sparse_accounting = (
                projection.get("source_accounting")
                == "direct-represented-v1"
            )
            if sparse_rows:
                rows = connection.execute(
                    """
                    SELECT staging.row_id, staging.dataset,
                           staging.source_row, staging.record_label,
                           staging.disposition,
                           COALESCE(exception.effective_disposition,
                                    staging.disposition),
                           COALESCE(exception.requires_review, FALSE),
                           COALESCE(exception.row_json, '')
                      FROM canonical_staging_row AS staging
                      LEFT JOIN quality_row_result AS exception
                        ON exception.run_id = ?
                       AND exception.row_id = staging.row_id
                     WHERE staging.run_id = ?
                     ORDER BY staging.ordinal
                    """,
                    [canonical_run_id, str(header[1])],
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT row_id, dataset, source_row, record_label,
                           base_disposition, effective_disposition,
                           requires_review, row_json
                      FROM quality_row_result
                     WHERE run_id = ? ORDER BY ordinal
                    """,
                    [canonical_run_id],
                ).fetchall()
            if sparse_accounting:
                accounting = connection.execute(
                    """
                    SELECT staging.ordinal, projection.dataset_id,
                           staging.source_row, 'REPRESENTED', '',
                           staging.row_id
                      FROM canonical_staging_row AS staging
                      JOIN canonical_prepared_projection AS projection
                        ON projection.run_id = staging.run_id
                       AND staging.ordinal >= projection.ordinal_start
                       AND staging.ordinal <
                           projection.ordinal_start + projection.row_count
                     WHERE staging.run_id = ?
                     ORDER BY projection.dataset_id,
                              staging.source_row
                    """,
                    [str(header[1])],
                ).fetchall()
                stored_accounting = connection.execute(
                    """
                    SELECT staging.row_json
                      FROM canonical_staging_row AS staging
                     WHERE staging.run_id = ?
                       AND staging.row_json != ''
                       AND NOT EXISTS (
                           SELECT 1
                             FROM canonical_prepared_projection AS projection
                            WHERE projection.run_id = staging.run_id
                              AND staging.ordinal >= projection.ordinal_start
                              AND staging.ordinal <
                                  projection.ordinal_start
                                  + projection.row_count
                       )
                     ORDER BY staging.ordinal
                    """,
                    [str(header[1])],
                ).fetchall()
            else:
                accounting = connection.execute(
                    """
                    SELECT ordinal, physical_dataset_id, source_row, state,
                           entry_json, NULL
                      FROM source_accounting_entry
                     WHERE run_id = ? ORDER BY ordinal
                    """,
                    [canonical_run_id],
                ).fetchall()
                stored_accounting = []
            issues = connection.execute(
                "SELECT issue_json FROM quality_issue WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            quarantine = connection.execute(
                "SELECT entry_json FROM quality_quarantine_entry WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            accounting_links = (
                []
                if sparse_accounting
                else connection.execute(
                    """
                    SELECT accounting_ordinal, row_id
                      FROM source_accounting_link
                     WHERE run_id = ?
                     ORDER BY accounting_ordinal, row_id
                    """,
                    [canonical_run_id],
                ).fetchall()
            )
        issue_objects = tuple(
            QualityIssue.from_dict(json.loads(str(item[0]))) for item in issues
        )
        issue_ids_by_row: dict[str, list[str]] = {}
        for issue in issue_objects:
            if issue.row_id is not None:
                issue_ids_by_row.setdefault(issue.row_id, []).append(
                    issue.issue_id
                )
        row_payloads = []
        for row in rows:
            if str(row[7]):
                row_payloads.append(json.loads(str(row[7])))
                continue
            row_payloads.append(
                QualityRowResult(
                    row_id=str(row[0]),
                    dataset=str(row[1]),
                    source_row=int(row[2]),
                    record_label=str(row[3]),
                    base_disposition=QualityDisposition(str(row[4])),
                    effective_disposition=QualityDisposition(str(row[5])),
                    issue_ids=tuple(
                        sorted(issue_ids_by_row.get(str(row[0]), ()))
                    ),
                    requires_review=bool(row[6]),
                ).to_portable_dict()
            )
        links_by_ordinal: dict[int, list[str]] = {}
        for ordinal, row_id in accounting_links:
            links_by_ordinal.setdefault(int(ordinal), []).append(str(row_id))
        accounting_payloads = []
        for row in accounting:
            if str(row[4]):
                accounting_payloads.append(json.loads(str(row[4])))
                continue
            accounting_payloads.append(
                {
                    "physical_dataset_id": str(row[1]),
                    "source_row": int(row[2]),
                    "state": str(row[3]),
                    "canonical_row_ids": (
                        [str(row[5])]
                        if row[5] is not None
                        else links_by_ordinal.get(int(row[0]), [])
                    ),
                }
            )
        for (row_json,) in stored_accounting:
            try:
                canonical_row = CanonicalRow.from_dict(
                    json.loads(str(row_json))
                )
                physical_sources = canonical_row.lineage.physical_sources
                if len(physical_sources) != 1:
                    raise ValueError(
                        "Sparse quality accounting requires direct lineage"
                    )
                physical_dataset_id, source_rows = next(
                    iter(physical_sources.items())
                )
                if len(source_rows) != 1:
                    raise ValueError(
                        "Sparse quality accounting requires one source row"
                    )
            except (TypeError, ValueError) as error:
                raise WorkspaceError(
                    "Stored quality source accounting is invalid"
                ) from error
            accounting_payloads.append(
                {
                    "physical_dataset_id": physical_dataset_id,
                    "source_row": source_rows[0],
                    "state": SourceAccountingState.REPRESENTED.value,
                    "canonical_row_ids": [canonical_row.row_id],
                }
            )
        accounting_payloads.sort(
            key=lambda item: (
                str(item["physical_dataset_id"]),
                int(item["source_row"]),
            )
        )
        payload = {
            "content_hash": str(header[0]),
            "project_id": project_id,
            "staging_run_id": str(header[1]),
            "staging_content_hash": str(header[2]),
            "ruleset_hash": str(header[3]),
            "mapping_hash": str(header[4]),
            "schema_hash": str(header[5]),
            "retention_context_hash": str(header[6]),
            "contract_version": int(header[7]),
            "evaluator_version": int(header[8]),
            "effective_dataset_hash": (
                str(header[10]) if header[10] is not None else None
            ),
            "row_results": row_payloads,
            "source_accounting": accounting_payloads,
            "issues": [item.to_portable_dict() for item in issue_objects],
            "quarantine": [json.loads(str(item[0])) for item in quarantine],
        }
        try:
            return QualityRun.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored quality evidence is invalid") from error
    def get_quality_review_page(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str = "",
        dataset: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> QualityReviewPage:
        """Read one bounded quality-review page with three grouped queries."""

        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Quality run identifier is invalid") from error
        if status not in {"", "ready", "review", "quarantined", "blocked"}:
            raise WorkspaceError("Quality review filter is invalid")
        if page < 1 or page_size < 1 or page_size > 250:
            raise WorkspaceError("Quality review page is invalid")
        conditions: list[str] = []
        filter_parameters: list[object] = []
        if dataset:
            conditions.append("dataset = ?")
            filter_parameters.append(dataset)
        if status == "ready":
            conditions.append(
                "effective_disposition IN ('CANDIDATE', 'REFERENCE')"
            )
            conditions.append("requires_review = FALSE")
        elif status == "review":
            conditions.append("requires_review = TRUE")
        elif status == "quarantined":
            conditions.append(
                "effective_disposition IN ('QUARANTINED', 'EXCLUDED')"
            )
        elif status == "blocked":
            conditions.append("effective_disposition = 'BLOCKED'")
        filter_predicate = (
            " AND ".join(conditions) if conditions else "TRUE"
        )
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            current = connection.execute(
                """
                SELECT run.staging_run_id, projection.projection_json
                  FROM quality_run AS run
                  LEFT JOIN quality_evidence_projection AS projection
                    ON projection.run_id = run.run_id
                 WHERE run.run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if current is None:
                raise WorkspaceError("Quality run was not found")
            sparse = current[1] is not None and (
                json.loads(str(current[1])).get("row_results")
                == "direct-defaults-v1"
            )
            if sparse:
                relation = """
                (
                    SELECT staging.row_id, staging.dataset,
                           staging.source_row, staging.record_label,
                           staging.disposition AS base_disposition,
                           COALESCE(exception.effective_disposition,
                                    staging.disposition)
                               AS effective_disposition,
                           COALESCE(exception.requires_review, FALSE)
                               AS requires_review,
                           COALESCE(exception.row_json, '') AS row_json
                      FROM canonical_staging_row AS staging
                      LEFT JOIN quality_row_result AS exception
                        ON exception.run_id = ?
                       AND exception.row_id = staging.row_id
                     WHERE staging.run_id = ?
                ) AS logical_quality
                """
                relation_parameters: list[object] = [
                    canonical_run_id,
                    str(current[0]),
                ]
            else:
                relation = "quality_row_result"
                filter_predicate = (
                    f"run_id = ? AND {filter_predicate}"
                )
                relation_parameters = [canonical_run_id]
            parameters = [*relation_parameters, *filter_parameters]
            count_row = connection.execute(
                f"SELECT COUNT(*) FROM {relation} WHERE {filter_predicate}",
                parameters,
            ).fetchone()
            matching_count = int(count_row[0]) if count_row else 0
            page_count = max(1, (matching_count + page_size - 1) // page_size)
            current_page = min(page, page_count)
            offset = (current_page - 1) * page_size
            rows = connection.execute(
                f"""
                SELECT row_id, dataset, source_row, record_label,
                       base_disposition, effective_disposition,
                       requires_review, row_json
                  FROM {relation}
                 WHERE {filter_predicate}
                 ORDER BY dataset, source_row, row_id
                 LIMIT ? OFFSET ?
                """,
                [*parameters, page_size, offset],
            ).fetchall()
            row_ids = tuple(str(item[0]) for item in rows)
            issues_by_row: dict[str, list[QualityIssue]] = {
                row_id: [] for row_id in row_ids
            }
            route_by_row: dict[str, str] = {}
            if row_ids:
                placeholders = ", ".join("?" for _ in row_ids)
                issue_rows = connection.execute(
                    f"""
                    SELECT issue_json FROM quality_issue
                     WHERE run_id = ? AND row_id IN ({placeholders})
                     ORDER BY issue_id
                    """,
                    [canonical_run_id, *row_ids],
                ).fetchall()
                for item in issue_rows:
                    issue = QualityIssue.from_dict(json.loads(str(item[0])))
                    if issue.row_id is not None:
                        issues_by_row[issue.row_id].append(issue)
                quarantine_rows = connection.execute(
                    f"""
                    SELECT entry_json FROM quality_quarantine_entry
                     WHERE run_id = ? AND row_id IN ({placeholders})
                     ORDER BY entry_id
                    """,
                    [canonical_run_id, *row_ids],
                ).fetchall()
                for item in quarantine_rows:
                    entry = QuarantineEntry.from_dict(json.loads(str(item[0])))
                    route_by_row.setdefault(entry.row_id, entry.correction_route)
            results = tuple(
                (
                    QualityRowResult.from_dict(json.loads(str(item[7])))
                    if str(item[7])
                    else QualityRowResult(
                        row_id=str(item[0]),
                        dataset=str(item[1]),
                        source_row=int(item[2]),
                        record_label=str(item[3]),
                        base_disposition=QualityDisposition(str(item[4])),
                        effective_disposition=QualityDisposition(str(item[5])),
                        issue_ids=tuple(
                            issue.issue_id
                            for issue in issues_by_row[str(item[0])]
                        ),
                        requires_review=bool(item[6]),
                    )
                )
                for item in rows
            )
        return QualityReviewPage(
            items=tuple(
                QualityReviewItem(
                    row=item,
                    issues=tuple(issues_by_row[item.row_id]),
                    correction_route=route_by_row.get(item.row_id, ""),
                )
                for item in results
            ),
            matching_count=matching_count,
            page=current_page,
            page_count=page_count,
        )
    @staticmethod
    def _insert_quality_evidence(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        run: QualityRun | StoredQualityRun,
    ) -> str:
        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("contract_version", run.contract_version)
        if run.contract_version >= 2:
            hasher.add_value(
                "effective_dataset_hash",
                run.effective_dataset_hash,
            )
        hasher.add_value("evaluator_version", run.evaluator_version)
        hasher.start_array("issues")
        for start in range(0, len(run.issues), QUALITY_ROW_BATCH_SIZE):
            batch = run.issues[start : start + QUALITY_ROW_BATCH_SIZE]
            values: list[list[object]] = []
            for offset, item in enumerate(batch):
                item_json = _canonical_json(item.to_portable_dict())
                hasher.add_encoded_array_item(item_json)
                values.append([
                    run_id, start + offset, item.issue_id, item.rule_id,
                    item.dataset, item.row_id, item.policy.value, item_json,
                ])
            connection.execute(
                """
                INSERT INTO quality_issue (
                    run_id, ordinal, issue_id, rule_id, dataset, row_id,
                    policy, issue_json
                )
                SELECT
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR)
                """,
                _columnar_parameters(values),
            )
        hasher.end_array()
        hasher.add_value("mapping_hash", run.mapping_hash)
        hasher.add_value("project_id", run.project_id)
        hasher.start_array("quarantine")
        for start in range(0, len(run.quarantine), QUALITY_ROW_BATCH_SIZE):
            batch = run.quarantine[start : start + QUALITY_ROW_BATCH_SIZE]
            values = []
            for offset, item in enumerate(batch):
                item_json = _canonical_json(item.to_portable_dict())
                hasher.add_encoded_array_item(item_json)
                values.append([
                    run_id, start + offset, item.entry_id, item.row_id,
                    item.rule_id, item_json,
                ])
            connection.execute(
                """
                INSERT INTO quality_quarantine_entry (
                    run_id, ordinal, entry_id, row_id, rule_id, entry_json,
                    superseded_by_run_id
                )
                SELECT
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    NULL
                """,
                _columnar_parameters(values),
            )
        hasher.end_array()
        hasher.add_value("retention_context_hash", run.retention_context_hash)
        hasher.start_array("row_results")
        row_ordinal = 0
        sparse_rows = getattr(
            run.row_results,
            "sparse_projection_contract",
            None,
        )
        sparse_accounting = getattr(
            run.source_accounting,
            "sparse_projection_contract",
            None,
        )
        if sparse_rows or sparse_accounting:
            connection.execute(
                """
                INSERT INTO quality_evidence_projection
                VALUES (?, 1, ?)
                """,
                [
                    run_id,
                    _canonical_json(
                        {
                            "row_results": sparse_rows,
                            "source_accounting": sparse_accounting,
                        }
                    ),
                ],
            )
        if isinstance(run, StoredQualityRun):
            row_batch_reader = getattr(run.row_results, "iter_batches", None)
            if not callable(row_batch_reader):
                raise WorkspaceError("Stored quality rows are not replayable")
            for batch in row_batch_reader(connection, QUALITY_ROW_BATCH_SIZE):
                batch_start_ordinal = row_ordinal

                def transport_rows():
                    for offset, item in enumerate(batch):
                        item_json = _canonical_json(item.to_portable_dict())
                        hasher.add_encoded_array_item(item_json)
                        if sparse_rows and not (
                            item.issue_ids
                            or item.requires_review
                            or item.base_disposition
                            not in {
                                QualityDisposition.CANDIDATE,
                                QualityDisposition.REFERENCE,
                            }
                            or item.effective_disposition
                            is not item.base_disposition
                        ):
                            continue
                        yield {
                            "ordinal": batch_start_ordinal + offset,
                            "row_id": item.row_id,
                            "dataset": item.dataset,
                            "source_row": item.source_row,
                            "record_label": item.record_label,
                            "base_disposition": item.base_disposition.value,
                            "effective_disposition": (
                                item.effective_disposition.value
                            ),
                            "requires_review": item.requires_review,
                            "row_json": "" if sparse_rows else item_json,
                        }

                for encoded_batch in iter_encoded_json_batches(
                    transport_rows(),
                    max_rows=QUALITY_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO quality_row_result (
                            run_id, ordinal, row_id, dataset, source_row,
                            record_label, base_disposition,
                            effective_disposition, requires_review, row_json
                        )
                        SELECT
                            ?, item.ordinal, item.row_id, item.dataset,
                            item.source_row, item.record_label,
                            item.base_disposition, item.effective_disposition,
                            item.requires_review, item.row_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            run_id,
                            encoded_batch.payload,
                            _QUALITY_ROW_RESULT_JSON_STRUCTURE,
                        ],
                    )
                row_ordinal += len(batch)
        else:
            for start in range(
                0,
                len(run.row_results),
                QUALITY_ROW_BATCH_SIZE,
            ):
                batch = run.row_results[
                    start : start + QUALITY_ROW_BATCH_SIZE
                ]
                values = []
                for offset, item in enumerate(batch):
                    item_json = _canonical_json(item.to_portable_dict())
                    hasher.add_encoded_array_item(item_json)
                    values.append([
                        run_id,
                        row_ordinal + offset,
                        item.row_id,
                        item.dataset,
                        item.source_row,
                        item.record_label,
                        item.base_disposition.value,
                        item.effective_disposition.value,
                        item.requires_review,
                        item_json,
                    ])
                connection.execute(
                    """
                    INSERT INTO quality_row_result (
                        run_id, ordinal, row_id, dataset, source_row,
                        record_label, base_disposition,
                        effective_disposition, requires_review, row_json
                    )
                    SELECT
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS BIGINT), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS BOOLEAN), CAST(unnest(?) AS VARCHAR)
                    """,
                    _columnar_parameters(values),
                )
                row_ordinal += len(batch)
        if row_ordinal != len(run.row_results):
            raise WorkspaceError("Quality row evidence is incomplete")
        hasher.end_array()
        hasher.add_value("ruleset_hash", run.ruleset_hash)
        hasher.add_value("schema_hash", run.schema_hash)
        hasher.start_array("source_accounting")
        accounting_ordinal = 0
        if isinstance(run, StoredQualityRun):
            accounting_batch_reader = getattr(
                run.source_accounting,
                "iter_batches",
                None,
            )
            if not callable(accounting_batch_reader):
                raise WorkspaceError(
                    "Stored quality source accounting is not replayable"
                )
            accounting_batches = accounting_batch_reader(
                connection,
                QUALITY_ROW_BATCH_SIZE,
            )
            for batch in accounting_batches:
                batch_start_ordinal = accounting_ordinal

                def entry_transport_rows():
                    for offset, item in enumerate(batch):
                        item_json = _canonical_json(item.to_portable_dict())
                        hasher.add_encoded_array_item(item_json)
                        if sparse_accounting:
                            continue
                        yield {
                            "ordinal": batch_start_ordinal + offset,
                            "physical_dataset_id": item.physical_dataset_id,
                            "source_row": item.source_row,
                            "state": item.state.value,
                            "entry_json": (
                                "" if sparse_accounting else item_json
                            ),
                        }

                inserted_entries = 0
                for encoded_batch in iter_encoded_json_batches(
                    entry_transport_rows(),
                    max_rows=QUALITY_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO source_accounting_entry (
                            run_id, ordinal, physical_dataset_id, source_row,
                            state, entry_json
                        )
                        SELECT
                            ?, item.ordinal, item.physical_dataset_id,
                            item.source_row, item.state, item.entry_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            run_id,
                            encoded_batch.payload,
                            _SOURCE_ACCOUNTING_ENTRY_JSON_STRUCTURE,
                        ],
                    )
                    inserted_entries += encoded_batch.row_count
                expected_entries = 0 if sparse_accounting else len(batch)
                if inserted_entries != expected_entries:
                    raise WorkspaceError(
                        "Quality source accounting batch is incomplete"
                    )

                link_rows = (
                    {
                        "accounting_ordinal": batch_start_ordinal + offset,
                        "row_id": row_id,
                    }
                    for offset, item in enumerate(batch)
                    for row_id in item.canonical_row_ids
                    if not sparse_accounting
                )
                expected_links = (
                    0
                    if sparse_accounting
                    else sum(
                        len(item.canonical_row_ids)
                        for item in batch
                    )
                )
                inserted_links = 0
                for encoded_batch in iter_encoded_json_batches(
                    link_rows,
                    max_rows=QUALITY_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO source_accounting_link (
                            run_id, accounting_ordinal, row_id
                        )
                        SELECT
                            ?, item.accounting_ordinal, item.row_id
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            run_id,
                            encoded_batch.payload,
                            _SOURCE_ACCOUNTING_LINK_JSON_STRUCTURE,
                        ],
                    )
                    inserted_links += encoded_batch.row_count
                if inserted_links != expected_links:
                    raise WorkspaceError(
                        "Quality source accounting links are incomplete"
                    )
                accounting_ordinal += len(batch)
        else:
            for start in range(
                0,
                len(run.source_accounting),
                QUALITY_ROW_BATCH_SIZE,
            ):
                batch = run.source_accounting[
                    start : start + QUALITY_ROW_BATCH_SIZE
                ]
                values = []
                for offset, item in enumerate(batch):
                    item_json = _canonical_json(item.to_portable_dict())
                    hasher.add_encoded_array_item(item_json)
                    values.append([
                        run_id,
                        accounting_ordinal + offset,
                        item.physical_dataset_id,
                        item.source_row,
                        item.state.value,
                        item_json,
                    ])
                connection.execute(
                    """
                    INSERT INTO source_accounting_entry (
                        run_id, ordinal, physical_dataset_id, source_row,
                        state, entry_json
                    )
                    SELECT
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR)
                    """,
                    _columnar_parameters(values),
                )
                links = [
                    [run_id, accounting_ordinal + offset, row_id]
                    for offset, item in enumerate(batch)
                    for row_id in item.canonical_row_ids
                ]
                if links:
                    connection.execute(
                        """
                        INSERT INTO source_accounting_link (
                            run_id, accounting_ordinal, row_id
                        )
                        SELECT
                            CAST(unnest(?) AS VARCHAR),
                            CAST(unnest(?) AS BIGINT),
                            CAST(unnest(?) AS VARCHAR)
                        """,
                        _columnar_parameters(links),
                    )
                accounting_ordinal += len(batch)
        if accounting_ordinal != len(run.source_accounting):
            raise WorkspaceError("Quality source accounting is incomplete")
        hasher.end_array()
        hasher.add_value("staging_content_hash", run.staging_content_hash)
        return hasher.finish()
    @staticmethod
    def _quality_summary(
        project_id: str,
        row: Sequence[object],
    ) -> QualityRunSummary:
        counts = json.loads(str(row[8]))
        return QualityRunSummary(
            run_id=str(row[0]),
            project_id=project_id,
            content_hash=str(row[1]),
            staging_run_id=str(row[2]),
            staging_content_hash=str(row[3]),
            ruleset_hash=str(row[4]),
            status=QualityRunStatus(str(row[5])),
            published_at=datetime.fromisoformat(str(row[6])),
            published_by=str(row[7]),
            ready_count=int(counts["ready_count"]),
            review_count=int(counts["review_count"]),
            quarantined_count=int(counts["quarantined_count"]),
            excluded_count=int(counts["excluded_count"]),
            blocked_count=int(counts["blocked_count"]),
            effective_dataset_run_id=(
                str(row[9]) if len(row) > 9 and row[9] is not None else None
            ),
            effective_dataset_hash=(
                str(row[10]) if len(row) > 10 and row[10] is not None else None
            ),
        )


def _quality_summary_counts(
    run: QualityRun | StoredQualityRun,
) -> dict[str, int]:
    """Accumulate publication counts without rescanning all evidence per field."""

    if isinstance(run, StoredQualityRun):
        return dict(run.summary_counts)

    counts = {
        "ready_count": 0,
        "review_count": 0,
        "quarantined_count": 0,
        "excluded_count": 0,
        "blocked_count": 0,
    }
    for item in run.row_results:
        if (
            item.effective_disposition
            in {QualityDisposition.CANDIDATE, QualityDisposition.REFERENCE}
            and not item.requires_review
        ):
            counts["ready_count"] += 1
        if item.requires_review:
            counts["review_count"] += 1
        if item.effective_disposition is QualityDisposition.QUARANTINED:
            counts["quarantined_count"] += 1
        elif item.effective_disposition is QualityDisposition.EXCLUDED:
            counts["excluded_count"] += 1
        elif item.effective_disposition is QualityDisposition.BLOCKED:
            counts["blocked_count"] += 1
    counts["blocked_count"] += sum(
        item.row_id is None and item.policy is QualityOutcomePolicy.BLOCK
        for item in run.issues
    )
    counts["review_count"] += sum(
        item.row_id is None and item.policy is QualityOutcomePolicy.WARNING
        for item in run.issues
    )
    counts["blocked_count"] += sum(
        item.state is SourceAccountingState.UNREPRESENTED
        for item in run.source_accounting
    )
    return counts

