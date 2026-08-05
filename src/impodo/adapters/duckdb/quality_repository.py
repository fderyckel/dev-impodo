"""DuckDB quality repository implementation."""

from __future__ import annotations

from .constants import QUALITY_ROW_BATCH_SIZE

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
from ...quality import (
    QUALITY_CONTRACT_VERSION,
    QUALITY_EVALUATOR_VERSION,
    QUALITY_RULESET_CONTRACT_VERSION,
    QualityIssue,
    QualityRuleSet,
    QualityReviewItem,
    QualityReviewPage,
    QualityRowResult,
    QualityRun,
    QualityRunStatus,
    QualityRunSummary,
    QuarantineEntry,
    retention_context_hash,
)
from ...workspace_errors import WorkspaceError
from .database import DuckDbDatabase
from .project_repository import ProjectRepository
from .repository import DuckDbRepository





from .serialization import _canonical_json


class QualityRepository(DuckDbRepository):
    """Persistence operations for quality repository."""

    def __init__(
        self,
        database: DuckDbDatabase,
        projects: ProjectRepository,
    ) -> None:
        super().__init__(database)
        self._projects = projects

    def get_current_quality_ruleset(
        self,
        project_id: str,
    ) -> QualityRuleSet | None:
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
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        created_at = datetime.now(timezone.utc)
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
                    revision=self._project_revision(connection),
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
        run: QualityRun,
        *,
        staging_run_id: str,
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
        run_payload = run.to_portable_dict()
        run_content_hash = str(run_payload["content_hash"])
        try:
            QualityRun.from_json(_canonical_json(run_payload))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Quality evidence is invalid") from error
        finally:
            del run_payload
        project = self._projects.get(project_id)
        if run.retention_context_hash != retention_context_hash(project):
            raise WorkspaceError(
                "Quality evidence no longer matches project ownership and retention"
            )
        database_path = self.project_directory(project_id) / "project.duckdb"
        published_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
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
                           published_at, published_by, summary_json
                      FROM quality_run
                     WHERE run_id = (
                         SELECT run_id FROM quality_current
                          WHERE singleton_id = 1
                     )
                    """
                ).fetchone()
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
                        summary_json, retired_at, retired_reason,
                        successor_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
                        _canonical_json(
                            {
                                "ready_count": run.ready_count,
                                "review_count": run.review_count,
                                "quarantined_count": run.quarantined_count,
                                "excluded_count": run.excluded_count,
                                "blocked_count": run.blocked_count,
                            }
                        ),
                    ],
                )
                self._insert_quality_evidence(connection, run_id, run)
                stored = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM quality_row_result WHERE run_id = ?),
                        (SELECT COUNT(*) FROM source_accounting_entry WHERE run_id = ?),
                        (SELECT COUNT(*) FROM quality_issue WHERE run_id = ?),
                        (SELECT COUNT(*) FROM quality_quarantine_entry WHERE run_id = ?),
                        (SELECT COUNT(*)
                           FROM quality_row_result AS quality
                           JOIN canonical_staging_row AS staging
                             ON staging.run_id = ?
                            AND staging.row_id = quality.row_id
                          WHERE quality.run_id = ?)
                    """,
                    [run_id, run_id, run_id, run_id, staging_run_id, run_id],
                ).fetchone()
                expected = (
                    len(run.row_results),
                    len(run.source_accounting),
                    len(run.issues),
                    len(run.quarantine),
                    len(run.row_results),
                )
                if stored is None or tuple(int(item) for item in stored) != expected:
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
                    UPDATE project
                       SET current_run_id = NULL,
                           approval_status = 'INVALIDATED'
                    """
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="QUALITY_RUN_PUBLISHED",
                    detail=(
                        f"run {run_id}: {run.ready_count} ready; "
                        f"{run.review_count} review; "
                        f"{run.quarantined_count} set aside; "
                        f"{run.blocked_count} setup"
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
            ready_count=run.ready_count,
            review_count=run.review_count,
            quarantined_count=run.quarantined_count,
            excluded_count=run.excluded_count,
            blocked_count=run.blocked_count,
        )
    def get_current_quality_summary(
        self,
        project_id: str,
    ) -> QualityRunSummary | None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute(
                """
                SELECT run.run_id, run.content_hash, run.staging_run_id,
                       run.staging_content_hash, run.ruleset_hash, run.status,
                       run.published_at, run.published_by, run.summary_json
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
        try:
            canonical_run_id = str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Quality run identifier is invalid") from error
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            header = connection.execute(
                """
                SELECT content_hash, staging_run_id, staging_content_hash,
                       ruleset_hash, mapping_hash, schema_hash,
                       retention_context_hash, contract_version,
                       evaluator_version
                  FROM quality_run
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if header is None:
                return None
            rows = connection.execute(
                "SELECT row_json FROM quality_row_result WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            accounting = connection.execute(
                "SELECT entry_json FROM source_accounting_entry WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            issues = connection.execute(
                "SELECT issue_json FROM quality_issue WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            quarantine = connection.execute(
                "SELECT entry_json FROM quality_quarantine_entry WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
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
            "row_results": [json.loads(str(item[0])) for item in rows],
            "source_accounting": [json.loads(str(item[0])) for item in accounting],
            "issues": [json.loads(str(item[0])) for item in issues],
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
        conditions = ["run_id = ?"]
        parameters: list[object] = [canonical_run_id]
        if dataset:
            conditions.append("dataset = ?")
            parameters.append(dataset)
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
        predicate = " AND ".join(conditions)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            current = connection.execute(
                """
                SELECT 1 FROM quality_run
                 WHERE run_id = ?
                """,
                [canonical_run_id],
            ).fetchone()
            if current is None:
                raise WorkspaceError("Quality run was not found")
            count_row = connection.execute(
                f"SELECT COUNT(*) FROM quality_row_result WHERE {predicate}",
                parameters,
            ).fetchone()
            matching_count = int(count_row[0]) if count_row else 0
            page_count = max(1, (matching_count + page_size - 1) // page_size)
            current_page = min(page, page_count)
            offset = (current_page - 1) * page_size
            rows = connection.execute(
                f"""
                SELECT row_json
                  FROM quality_row_result
                 WHERE {predicate}
                 ORDER BY dataset, source_row, row_id
                 LIMIT ? OFFSET ?
                """,
                [*parameters, page_size, offset],
            ).fetchall()
            results = tuple(
                QualityRowResult.from_dict(json.loads(str(item[0])))
                for item in rows
            )
            row_ids = tuple(item.row_id for item in results)
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
        run: QualityRun,
    ) -> None:
        for start in range(0, len(run.row_results), QUALITY_ROW_BATCH_SIZE):
            batch = run.row_results[start : start + QUALITY_ROW_BATCH_SIZE]
            values = [
                [run_id, start + offset, item.row_id, item.dataset,
                 item.source_row, item.effective_disposition.value,
                 item.requires_review, _canonical_json(item.to_portable_dict())]
                for offset, item in enumerate(batch)
            ]
            connection.execute(
                """
                INSERT INTO quality_row_result (
                    run_id, ordinal, row_id, dataset, source_row,
                    effective_disposition, requires_review, row_json
                )
                SELECT
                    json_extract_string(value, '$[0]'),
                    CAST(json_extract(value, '$[1]') AS BIGINT),
                    json_extract_string(value, '$[2]'),
                    json_extract_string(value, '$[3]'),
                    CAST(json_extract(value, '$[4]') AS BIGINT),
                    json_extract_string(value, '$[5]'),
                    CAST(json_extract(value, '$[6]') AS BOOLEAN),
                    json_extract_string(value, '$[7]')
                  FROM json_each(?)
                """,
                [_canonical_json(values)],
            )
        for start in range(0, len(run.issues), QUALITY_ROW_BATCH_SIZE):
            batch = run.issues[start : start + QUALITY_ROW_BATCH_SIZE]
            values = [
                [run_id, start + offset, item.issue_id, item.rule_id,
                 item.dataset, item.row_id, item.policy.value,
                 _canonical_json(item.to_portable_dict())]
                for offset, item in enumerate(batch)
            ]
            connection.execute(
                """
                INSERT INTO quality_issue (
                    run_id, ordinal, issue_id, rule_id, dataset, row_id,
                    policy, issue_json
                )
                SELECT
                    json_extract_string(value, '$[0]'),
                    CAST(json_extract(value, '$[1]') AS BIGINT),
                    json_extract_string(value, '$[2]'),
                    json_extract_string(value, '$[3]'),
                    json_extract_string(value, '$[4]'),
                    json_extract_string(value, '$[5]'),
                    json_extract_string(value, '$[6]'),
                    json_extract_string(value, '$[7]')
                  FROM json_each(?)
                """,
                [_canonical_json(values)],
            )
        for start in range(0, len(run.source_accounting), QUALITY_ROW_BATCH_SIZE):
            batch = run.source_accounting[start : start + QUALITY_ROW_BATCH_SIZE]
            values = [
                [run_id, start + offset, item.physical_dataset_id,
                 item.source_row, item.state.value,
                 _canonical_json(item.to_portable_dict())]
                for offset, item in enumerate(batch)
            ]
            connection.execute(
                """
                INSERT INTO source_accounting_entry (
                    run_id, ordinal, physical_dataset_id, source_row,
                    state, entry_json
                )
                SELECT
                    json_extract_string(value, '$[0]'),
                    CAST(json_extract(value, '$[1]') AS BIGINT),
                    json_extract_string(value, '$[2]'),
                    CAST(json_extract(value, '$[3]') AS BIGINT),
                    json_extract_string(value, '$[4]'),
                    json_extract_string(value, '$[5]')
                  FROM json_each(?)
                """,
                [_canonical_json(values)],
            )
            links = [
                [run_id, start + offset, row_id]
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
                        json_extract_string(value, '$[0]'),
                        CAST(json_extract(value, '$[1]') AS BIGINT),
                        json_extract_string(value, '$[2]')
                      FROM json_each(?)
                    """,
                    [_canonical_json(links)],
                )
        for start in range(0, len(run.quarantine), QUALITY_ROW_BATCH_SIZE):
            batch = run.quarantine[start : start + QUALITY_ROW_BATCH_SIZE]
            values = [
                [run_id, start + offset, item.entry_id, item.row_id,
                 item.rule_id, _canonical_json(item.to_portable_dict()), None]
                for offset, item in enumerate(batch)
            ]
            connection.execute(
                """
                INSERT INTO quality_quarantine_entry (
                    run_id, ordinal, entry_id, row_id, rule_id, entry_json,
                    superseded_by_run_id
                )
                SELECT
                    json_extract_string(value, '$[0]'),
                    CAST(json_extract(value, '$[1]') AS BIGINT),
                    json_extract_string(value, '$[2]'),
                    json_extract_string(value, '$[3]'),
                    json_extract_string(value, '$[4]'),
                    json_extract_string(value, '$[5]'),
                    json_extract_string(value, '$[6]')
                  FROM json_each(?)
                """,
                [_canonical_json(values)],
            )
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
        )
