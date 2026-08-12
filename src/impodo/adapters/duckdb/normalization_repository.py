"""DuckDB persistence for Stage-G review decisions and eligible-data freeze.

The repository stores immutable evaluation effects/groups beside a versioned
``DryRun`` lifecycle. Group decisions use optimistic lifecycle versions; final
approval freezes the exact eligible-dataset hash for Stage H consumption.
"""

from __future__ import annotations

from .constants import (
    DUCKDB_JSON_BATCH_MAX_BYTES,
    NORMALIZATION_ROW_BATCH_SIZE,
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
from ...projects import ProjectNotFoundError
from ...governance import DryRun
from ...normalization import (
    NORMALIZATION_CONTRACT_VERSION,
    NORMALIZATION_EVALUATOR_VERSION,
    NormalizationEvaluation,
    NormalizationOutcome,
    NormalizationReviewGroup,
    NormalizationRunSummary,
    StoredNormalizationEvaluation,
    start_dry_run,
)
from ...quality import retention_context_hash
from ...workspace_errors import WorkspaceError
from ...domain.serialization import CanonicalJsonObjectHasher
from .database import DuckDbDatabase
from .project_repository import ProjectRepository
from .repository import DuckDbRepository





from .serialization import (
    _canonical_json,
    _columnar_parameters,
    iter_encoded_json_batches,
)


_NORMALIZATION_EFFECT_JSON_STRUCTURE = """[{
    "effect_id":"VARCHAR",
    "group_id":"VARCHAR",
    "row_id":"VARCHAR",
    "dataset":"VARCHAR",
    "source_row":"BIGINT",
    "target_field":"VARCHAR",
    "eligible":"BOOLEAN",
    "effect_json":"VARCHAR"
}]"""


class NormalizationRepository(DuckDbRepository):
    """Implement publication, concurrent review, approval, and freeze."""

    def __init__(
        self,
        database: DuckDbDatabase,
        projects: ProjectRepository,
    ) -> None:
        super().__init__(database)
        self._projects = projects

    def publish_normalization_run(
        self,
        project_id: str,
        evaluation: NormalizationEvaluation | StoredNormalizationEvaluation,
        *,
        staging_run_id: str,
        quality_run_id: str,
        source_hashes: dict[str, str],
        actor: Actor,
    ) -> NormalizationRunSummary:
        """Publish complete prepared-data review evidence without Odoo access."""

        if evaluation.project_id != project_id:
            raise WorkspaceError("Prepared review belongs to another project")
        if (
            evaluation.contract_version != NORMALIZATION_CONTRACT_VERSION
            or evaluation.evaluator_version != NORMALIZATION_EVALUATOR_VERSION
        ):
            raise WorkspaceError("Prepared review must be regenerated")
        project = self._projects.get(project_id)
        if evaluation.retention_context_hash != retention_context_hash(project):
            raise WorkspaceError(
                "Prepared review no longer matches project ownership and retention"
            )
        database_path = self.project_directory(project_id) / "project.duckdb"
        published_at = datetime.now(timezone.utc)
        run_id = str(uuid4())
        changed_record_count = evaluation.changed_record_count
        try:
            dry_run = start_dry_run(
                evaluation,
                run_id=run_id,
                source_hashes=source_hashes,
            )
        except ValueError as error:
            raise WorkspaceError(
                "Prepared review source evidence is invalid"
            ) from error
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                current_inputs = connection.execute(
                    """
                    SELECT staging.run_id, staging.content_hash,
                           quality.run_id, quality.content_hash,
                           staging.mapping_hash, staging.schema_hash,
                           quality.effective_dataset_run_id,
                           quality.effective_dataset_hash
                      FROM canonical_staging_current AS staging_current
                      JOIN canonical_staging_run AS staging
                        ON staging.run_id = staging_current.run_id
                      JOIN quality_current AS quality_current
                        ON quality_current.singleton_id = 1
                      JOIN quality_run AS quality
                        ON quality.run_id = quality_current.run_id
                     WHERE staging_current.singleton_id = 1
                       AND staging.status = 'PUBLISHED'
                       AND quality.status = 'PUBLISHED'
                    """
                ).fetchone()
                if current_inputs is None or (
                    str(current_inputs[0]) != staging_run_id
                    or str(current_inputs[1]) != evaluation.staging_content_hash
                    or str(current_inputs[2]) != quality_run_id
                    or str(current_inputs[3]) != evaluation.quality_content_hash
                    or str(current_inputs[4]) != evaluation.mapping_hash
                    or str(current_inputs[5]) != evaluation.schema_hash
                    or (
                        str(current_inputs[7])
                        if current_inputs[7] is not None
                        else None
                    ) != evaluation.effective_dataset_hash
                ):
                    raise WorkspaceError(
                        "Prepared review no longer matches the current data"
                    )
                current = connection.execute(
                    self._normalization_summary_query(
                        "WHERE run.run_id = (SELECT run_id FROM normalization_current WHERE singleton_id = 1)"
                    )
                ).fetchone()
                evaluation_content_hash = self._insert_normalization_evidence(
                    connection,
                    run_id,
                    evaluation,
                )
                if current is not None and str(current[1]) == evaluation_content_hash:
                    connection.rollback()
                    return self._normalization_summary(project_id, current)

                sparse_quality = connection.execute(
                    """
                    SELECT 1 FROM quality_evidence_projection
                     WHERE run_id = ?
                    """,
                    [quality_run_id],
                ).fetchone()
                if sparse_quality is not None:
                    counts = connection.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (
                                WHERE COALESCE(
                                    exception.effective_disposition,
                                    staging.disposition
                                ) IN ('CANDIDATE', 'REFERENCE')
                            ),
                            COUNT(*) FILTER (
                                WHERE COALESCE(
                                    exception.effective_disposition,
                                    staging.disposition
                                ) IN ('QUARANTINED', 'EXCLUDED')
                            )
                          FROM canonical_staging_row AS staging
                          LEFT JOIN quality_row_result AS exception
                            ON exception.run_id = ?
                           AND exception.row_id = staging.row_id
                         WHERE staging.run_id = ?
                        """,
                        [quality_run_id, staging_run_id],
                    ).fetchone()
                else:
                    counts = connection.execute(
                        """
                        SELECT
                            SUM(CASE WHEN effective_disposition IN ('CANDIDATE', 'REFERENCE') THEN 1 ELSE 0 END),
                            SUM(CASE WHEN effective_disposition IN ('QUARANTINED', 'EXCLUDED') THEN 1 ELSE 0 END)
                          FROM quality_row_result
                         WHERE run_id = ?
                        """,
                        [quality_run_id],
                    ).fetchone()
                eligible_count = int(counts[0] or 0) if counts else 0
                set_aside_count = int(counts[1] or 0) if counts else 0
                automatic_group_count = sum(
                    group.eligible_count > 0
                    and group.outcome is NormalizationOutcome.AUTOMATIC
                    for group in evaluation.groups
                )
                decision_group_count = sum(
                    group.requires_decision for group in evaluation.groups
                )

                connection.execute(
                    """
                    INSERT INTO normalization_run (
                        run_id, content_hash, staging_run_id,
                        staging_content_hash, quality_run_id,
                        quality_content_hash, mapping_hash, schema_hash,
                        policy_hash, retention_context_hash,
                        eligible_dataset_hash, contract_version,
                        evaluator_version, status, lifecycle_version,
                        published_at, published_by, eligible_record_count,
                        changed_record_count, automatic_group_count,
                        decision_group_count, set_aside_record_count,
                        evaluation_json, dry_run_json,
                        effective_dataset_run_id, effective_dataset_hash,
                        retired_at,
                        retired_reason, successor_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                    """,
                    [
                        run_id,
                        evaluation_content_hash,
                        staging_run_id,
                        evaluation.staging_content_hash,
                        quality_run_id,
                        evaluation.quality_content_hash,
                        evaluation.mapping_hash,
                        evaluation.schema_hash,
                        evaluation.policy_hash,
                        evaluation.retention_context_hash,
                        evaluation.eligible_dataset_hash,
                        evaluation.contract_version,
                        evaluation.evaluator_version,
                        dry_run.status.value,
                        1,
                        published_at.isoformat(),
                        actor.identity.display_name,
                        eligible_count,
                        changed_record_count,
                        automatic_group_count,
                        decision_group_count,
                        set_aside_count,
                        self._normalization_evaluation_header(evaluation),
                        dry_run.to_json(),
                        (
                            str(current_inputs[6])
                            if current_inputs[6] is not None
                            else None
                        ),
                        evaluation.effective_dataset_hash,
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO normalization_transition
                    VALUES (?, 1, 'PREPARED_REVIEW_PUBLISHED', ?, ?, ?)
                    """,
                    [
                        run_id,
                        published_at.isoformat(),
                        actor.identity.display_name,
                        dry_run.to_json(),
                    ],
                )
                if current is not None:
                    connection.execute(
                        """
                        UPDATE normalization_run
                           SET status = 'SUPERSEDED', retired_at = ?,
                               retired_reason = 'NEW_PREPARED_REVIEW',
                               successor_run_id = ?
                         WHERE run_id = ?
                        """,
                        [published_at.isoformat(), run_id, str(current[0])],
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO normalization_current VALUES (1, ?)",
                    [run_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="PREPARED_REVIEW_PUBLISHED",
                    detail=(
                        f"run {run_id}: {eligible_count} eligible; "
                        f"{changed_record_count} changed; "
                        f"{decision_group_count} decision group(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_current_normalization_summary(project_id)
        if summary is None:
            raise WorkspaceError("Prepared review was not published")
        return summary
    def get_current_normalization_summary(
        self,
        project_id: str,
    ) -> NormalizationRunSummary | None:
        """Return the current non-retired review run's lifecycle projection."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                self._normalization_summary_query(
                    """WHERE run.run_id = (
                           SELECT run_id FROM normalization_current
                            WHERE singleton_id = 1
                       )
                       AND run.status NOT IN ('INVALIDATED', 'SUPERSEDED')"""
                )
            ).fetchone()
        return self._normalization_summary(project_id, row) if row else None
    def get_normalization_evaluation(
        self,
        project_id: str,
        run_id: str,
    ) -> NormalizationEvaluation | None:
        """Reassemble immutable effect/group evidence and verify its hash."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                "SELECT content_hash, evaluation_json FROM normalization_run WHERE run_id = ?",
                [canonical_run_id],
            ).fetchone()
            effects = connection.execute(
                "SELECT effect_json FROM normalization_effect WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            groups = connection.execute(
                "SELECT group_json FROM normalization_group WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
        if row is None:
            return None
        try:
            payload = json.loads(str(row[1]))
            payload["content_hash"] = str(row[0])
            payload["effects"] = [json.loads(str(item[0])) for item in effects]
            payload["groups"] = [json.loads(str(item[0])) for item in groups]
            return NormalizationEvaluation.from_dict(payload)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared review is invalid") from error
    def get_normalization_dry_run(
        self,
        project_id: str,
        run_id: str,
    ) -> DryRun | None:
        """Load the current serialized decision state for one run."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                "SELECT dry_run_json FROM normalization_run WHERE run_id = ?",
                [canonical_run_id],
            ).fetchone()
        if row is None:
            return None
        try:
            return DryRun.from_json(str(row[0]))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared review decision is invalid") from error
    def get_normalization_review_groups(
        self,
        project_id: str,
        run_id: str,
    ) -> tuple[tuple[NormalizationReviewGroup, ...], int]:
        """Load bounded group summaries and count routine-change records in SQL."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            rows = connection.execute(
                "SELECT group_json FROM normalization_group WHERE run_id = ? ORDER BY ordinal",
                [canonical_run_id],
            ).fetchall()
            count = connection.execute(
                """
                SELECT COUNT(DISTINCT effect.row_id)
                  FROM normalization_effect AS effect
                  JOIN normalization_group AS group_row
                    ON group_row.run_id = effect.run_id
                   AND group_row.group_id = effect.group_id
                 WHERE effect.run_id = ?
                   AND effect.eligible = TRUE
                   AND group_row.requires_decision = FALSE
                """,
                [canonical_run_id],
            ).fetchone()
        return (
            tuple(
                self._normalization_group_from_json(str(item[0]))
                for item in rows
            ),
            int(count[0]) if count else 0,
        )
    def decide_normalization_group(
        self,
        project_id: str,
        run_id: str,
        group_id: str,
        *,
        approve: bool,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        """Record one manager decision with an optimistic lifecycle check."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        decided_at = datetime.now(timezone.utc)
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT run.dry_run_json, run.lifecycle_version,
                           group_row.group_json
                      FROM normalization_current AS current
                      JOIN normalization_run AS run ON run.run_id = current.run_id
                      JOIN normalization_group AS group_row
                        ON group_row.run_id = run.run_id
                       AND group_row.group_id = ?
                     WHERE current.singleton_id = 1
                       AND run.run_id = ?
                    """,
                    [group_id, canonical_run_id],
                ).fetchone()
                if row is None:
                    raise WorkspaceError("This prepared change is no longer current")
                if int(row[1]) != expected_version:
                    raise WorkspaceError(
                        "Prepared data was reviewed in another browser window. Refresh and try again."
                    )
                dry_run = DryRun.from_json(str(row[0]))
                group = self._normalization_group_from_json(str(row[2]))
                updated = (
                    dry_run.approve_group(
                        group.decision_key,
                        actor=actor,
                        decided_at=decided_at,
                        reason=reason,
                    )
                    if approve
                    else dry_run.reject_group(
                        group.decision_key,
                        actor=actor,
                        decided_at=decided_at,
                        reason=reason,
                    )
                )
                self._save_normalization_transition(
                    connection,
                    canonical_run_id,
                    expected_version=expected_version,
                    dry_run=updated,
                    event_type=("PREPARED_CHANGE_ACCEPTED" if approve else "PREPARED_CHANGE_SENT_BACK"),
                    actor=actor,
                    occurred_at=decided_at,
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type=("PREPARED_CHANGE_ACCEPTED" if approve else "PREPARED_CHANGE_SENT_BACK"),
                    detail=f"run {canonical_run_id}: group {group_id}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_current_normalization_summary(project_id)
        if summary is None:
            raise WorkspaceError("Prepared review is no longer current")
        return summary
    def approve_and_freeze_normalization(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        """Approve the full prepared dataset and bind its exact eligible hash."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        approved_at = datetime.now(timezone.utc)
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT run.dry_run_json, run.lifecycle_version,
                           run.eligible_dataset_hash
                      FROM normalization_current AS current
                      JOIN normalization_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1 AND run.run_id = ?
                    """,
                    [canonical_run_id],
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Prepared review is no longer current")
                if int(row[1]) != expected_version:
                    raise WorkspaceError(
                        "Prepared data was reviewed in another browser window. Refresh and try again."
                    )
                dry_run = DryRun.from_json(str(row[0]))
                with_groups_approved = dry_run.approve_all_required_groups(
                    actor=actor,
                    decided_at=approved_at,
                    reason=reason,
                )
                updated = with_groups_approved.approve(
                    actor=actor,
                    approved_at=approved_at,
                    reason=reason,
                ).freeze(canonical_dataset_hash=str(row[2]))
                self._save_normalization_transition(
                    connection,
                    canonical_run_id,
                    expected_version=expected_version,
                    dry_run=updated,
                    event_type="PREPARED_DATA_APPROVED",
                    actor=actor,
                    occurred_at=approved_at,
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="PREPARED_DATA_APPROVED",
                    detail=(
                        f"run {canonical_run_id}: accepted "
                        f"{len(updated.approved_groups)} required group(s); "
                        f"eligible dataset {row[2]}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_current_normalization_summary(project_id)
        if summary is None:
            raise WorkspaceError("Prepared review is no longer current")
        return summary

    def reopen_normalization_review(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_version: int,
        actor: Actor,
        reason: str = "",
    ) -> NormalizationRunSummary:
        """Reopen the current review while retaining prior audit transitions."""

        canonical_run_id = self._normalization_run_id(run_id)
        database_path = self.project_directory(project_id) / "project.duckdb"
        reopened_at = datetime.now(timezone.utc)
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT run.dry_run_json, run.lifecycle_version
                      FROM normalization_current AS current
                      JOIN normalization_run AS run ON run.run_id = current.run_id
                     WHERE current.singleton_id = 1 AND run.run_id = ?
                    """,
                    [canonical_run_id],
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Prepared review is no longer current")
                if int(row[1]) != expected_version:
                    raise WorkspaceError(
                        "Prepared data was reviewed in another browser window. Refresh and try again."
                    )
                dry_run = DryRun.from_json(str(row[0]))
                updated = dry_run.reopen_review()
                self._save_normalization_transition(
                    connection,
                    canonical_run_id,
                    expected_version=expected_version,
                    dry_run=updated,
                    event_type="PREPARED_REVIEW_REOPENED",
                    actor=actor,
                    occurred_at=reopened_at,
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._project_revision(connection),
                    event_type="PREPARED_REVIEW_REOPENED",
                    detail=f"run {canonical_run_id}: {reason.strip()}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        summary = self.get_current_normalization_summary(project_id)
        if summary is None:
            raise WorkspaceError("Prepared review is no longer current")
        return summary

    @staticmethod
    def _normalization_evaluation_header(
        evaluation: NormalizationEvaluation | StoredNormalizationEvaluation,
    ) -> str:
        return _canonical_json(
            {
                "contract_version": evaluation.contract_version,
                "evaluator_version": evaluation.evaluator_version,
                "project_id": evaluation.project_id,
                "staging_content_hash": evaluation.staging_content_hash,
                "quality_content_hash": evaluation.quality_content_hash,
                "mapping_hash": evaluation.mapping_hash,
                "schema_hash": evaluation.schema_hash,
                "policy_hash": evaluation.policy_hash,
                "retention_context_hash": evaluation.retention_context_hash,
                "eligible_dataset_hash": evaluation.eligible_dataset_hash,
                "effective_dataset_hash": evaluation.effective_dataset_hash,
            }
        )
    @staticmethod
    def _insert_normalization_evidence(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        evaluation: NormalizationEvaluation | StoredNormalizationEvaluation,
    ) -> str:
        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("contract_version", evaluation.contract_version)
        if evaluation.contract_version >= 2:
            hasher.add_value(
                "effective_dataset_hash",
                evaluation.effective_dataset_hash,
            )
        hasher.start_array("effects")
        if isinstance(evaluation, StoredNormalizationEvaluation):
            connection.execute(
                """
                CREATE TEMP TABLE normalization_pending_effect (
                    effect_id VARCHAR PRIMARY KEY,
                    group_id VARCHAR NOT NULL,
                    row_id VARCHAR NOT NULL,
                    dataset VARCHAR NOT NULL,
                    source_row BIGINT NOT NULL,
                    target_field VARCHAR NOT NULL,
                    eligible BOOLEAN NOT NULL,
                    effect_json VARCHAR NOT NULL
                )
                """
            )
            batch_reader = getattr(evaluation.effects, "iter_batches", None)
            if not callable(batch_reader):
                raise WorkspaceError(
                    "Stored normalization effects are not replayable"
                )
            effect_count = 0
            for batch in batch_reader(connection, NORMALIZATION_ROW_BATCH_SIZE):
                transport_rows = (
                    {
                        "effect_id": item.effect_id,
                        "group_id": item.group_id,
                        "row_id": item.row_id,
                        "dataset": item.dataset,
                        "source_row": item.source_row,
                        "target_field": item.target_field,
                        "eligible": item.eligible,
                        "effect_json": _canonical_json(
                            item.to_portable_dict()
                        ),
                    }
                    for item in batch
                )
                for encoded_batch in iter_encoded_json_batches(
                    transport_rows,
                    max_rows=NORMALIZATION_ROW_BATCH_SIZE,
                    max_bytes=DUCKDB_JSON_BATCH_MAX_BYTES,
                ):
                    connection.execute(
                        """
                        INSERT INTO normalization_pending_effect
                        SELECT
                            item.effect_id,
                            item.group_id,
                            item.row_id,
                            item.dataset,
                            item.source_row,
                            item.target_field,
                            item.eligible,
                            item.effect_json
                          FROM (
                            SELECT UNNEST(
                                from_json_strict(CAST(? AS JSON), ?)
                            ) AS item
                          )
                        """,
                        [
                            encoded_batch.payload,
                            _NORMALIZATION_EFFECT_JSON_STRUCTURE,
                        ],
                    )
                    effect_count += encoded_batch.row_count
            if effect_count != evaluation.effect_count:
                raise WorkspaceError("Stored normalization effects are incomplete")
            connection.execute(
                """
                INSERT INTO normalization_effect (
                    run_id, ordinal, effect_id, group_id, row_id, dataset,
                    source_row, target_field, eligible, effect_json
                )
                SELECT
                    ?, ROW_NUMBER() OVER (ORDER BY effect_id) - 1,
                    effect_id, group_id, row_id, dataset, source_row,
                    target_field, eligible, effect_json
                  FROM normalization_pending_effect
                """,
                [run_id],
            )
            cursor = connection.execute(
                """
                SELECT effect_json
                  FROM normalization_effect
                 WHERE run_id = ?
                 ORDER BY ordinal
                """,
                [run_id],
            )
            hashed_effect_count = 0
            while batch := cursor.fetchmany(NORMALIZATION_ROW_BATCH_SIZE):
                for (effect_json,) in batch:
                    hasher.add_encoded_array_item(str(effect_json))
                    hashed_effect_count += 1
            if hashed_effect_count != evaluation.effect_count:
                raise WorkspaceError("Stored normalization effect order is incomplete")
        else:
            for start in range(
                0,
                len(evaluation.effects),
                NORMALIZATION_ROW_BATCH_SIZE,
            ):
                batch = evaluation.effects[
                    start : start + NORMALIZATION_ROW_BATCH_SIZE
                ]
                values = []
                for offset, item in enumerate(batch):
                    item_json = _canonical_json(item.to_portable_dict())
                    hasher.add_encoded_array_item(item_json)
                    values.append([
                        run_id,
                        start + offset,
                        item.effect_id,
                        item.group_id,
                        item.row_id,
                        item.dataset,
                        item.source_row,
                        item.target_field,
                        item.eligible,
                        item_json,
                    ])
                connection.execute(
                    """
                    INSERT INTO normalization_effect (
                        run_id, ordinal, effect_id, group_id, row_id, dataset,
                        source_row, target_field, eligible, effect_json
                    )
                    SELECT
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS BIGINT), CAST(unnest(?) AS VARCHAR),
                        CAST(unnest(?) AS BOOLEAN), CAST(unnest(?) AS VARCHAR)
                    """,
                    _columnar_parameters(values),
                )
        hasher.end_array()
        hasher.add_value("eligible_dataset_hash", evaluation.eligible_dataset_hash)
        hasher.add_value("evaluator_version", evaluation.evaluator_version)
        hasher.start_array("groups")
        for start in range(0, len(evaluation.groups), NORMALIZATION_ROW_BATCH_SIZE):
            batch = evaluation.groups[start : start + NORMALIZATION_ROW_BATCH_SIZE]
            values = []
            for offset, item in enumerate(batch):
                item_json = _canonical_json(item.to_portable_dict())
                hasher.add_encoded_array_item(item_json)
                values.append([
                    run_id,
                    start + offset,
                    item.group_id,
                    item.kind.value,
                    item.outcome.value,
                    item.dataset,
                    item.target_field,
                    item.requires_decision,
                    item_json,
                ])
            connection.execute(
                """
                INSERT INTO normalization_group (
                    run_id, ordinal, group_id, kind, outcome, dataset,
                    target_field, requires_decision, group_json
                )
                SELECT
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BIGINT),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS VARCHAR),
                    CAST(unnest(?) AS VARCHAR), CAST(unnest(?) AS BOOLEAN),
                    CAST(unnest(?) AS VARCHAR)
                """,
                _columnar_parameters(values),
            )
        hasher.end_array()
        hasher.add_value("mapping_hash", evaluation.mapping_hash)
        hasher.add_value("policy_hash", evaluation.policy_hash)
        hasher.add_value("project_id", evaluation.project_id)
        hasher.add_value("quality_content_hash", evaluation.quality_content_hash)
        hasher.add_value(
            "retention_context_hash",
            evaluation.retention_context_hash,
        )
        hasher.add_value("schema_hash", evaluation.schema_hash)
        hasher.add_value("staging_content_hash", evaluation.staging_content_hash)
        return hasher.finish()
    @staticmethod
    def _normalization_summary_query(where: str) -> str:
        return f"""
            SELECT run.run_id, run.content_hash, run.staging_run_id,
                   run.staging_content_hash, run.quality_run_id,
                   run.quality_content_hash, run.eligible_dataset_hash,
                   run.status, run.lifecycle_version, run.published_at,
                   run.published_by, run.eligible_record_count,
                   run.changed_record_count, run.automatic_group_count,
                   run.decision_group_count, run.set_aside_record_count,
                   run.dry_run_json, run.effective_dataset_run_id,
                   run.effective_dataset_hash
              FROM normalization_run AS run
              {where}
        """
    @staticmethod
    def _normalization_summary(
        project_id: str,
        row: Sequence[object],
    ) -> NormalizationRunSummary:
        try:
            dry_run = DryRun.from_json(str(row[16]))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared review decision is invalid") from error
        return NormalizationRunSummary(
            run_id=str(row[0]),
            project_id=project_id,
            content_hash=str(row[1]),
            staging_run_id=str(row[2]),
            staging_content_hash=str(row[3]),
            quality_run_id=str(row[4]),
            quality_content_hash=str(row[5]),
            eligible_dataset_hash=str(row[6]),
            status=str(row[7]),
            lifecycle_version=int(row[8]),
            published_at=datetime.fromisoformat(str(row[9])),
            published_by=str(row[10]),
            eligible_record_count=int(row[11]),
            changed_record_count=int(row[12]),
            automatic_group_count=int(row[13]),
            decision_group_count=int(row[14]),
            reviewed_group_count=len(dry_run.group_decisions),
            set_aside_record_count=int(row[15]),
            effective_dataset_run_id=(
                str(row[17]) if len(row) > 17 and row[17] is not None else None
            ),
            effective_dataset_hash=(
                str(row[18]) if len(row) > 18 and row[18] is not None else None
            ),
        )
    @staticmethod
    def _normalization_run_id(run_id: str) -> str:
        try:
            return str(UUID(run_id))
        except (ValueError, AttributeError) as error:
            raise WorkspaceError("Prepared review identifier is invalid") from error
    @staticmethod
    def _normalization_group_from_json(value: str) -> NormalizationReviewGroup:
        try:
            return NormalizationReviewGroup.from_dict(json.loads(value))
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored prepared change is invalid") from error
    @staticmethod
    def _save_normalization_transition(
        connection: duckdb.DuckDBPyConnection,
        run_id: str,
        *,
        expected_version: int,
        dry_run: DryRun,
        event_type: str,
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        next_version = expected_version + 1
        updated = connection.execute(
            """
            UPDATE normalization_run
               SET status = ?, lifecycle_version = ?, dry_run_json = ?
             WHERE run_id = ? AND lifecycle_version = ?
            RETURNING run_id
            """,
            [
                dry_run.status.value,
                next_version,
                dry_run.to_json(),
                run_id,
                expected_version,
            ],
        ).fetchone()
        if updated is None:
            raise WorkspaceError(
                "Prepared data was reviewed in another browser window. Refresh and try again."
            )
        connection.execute(
            """
            INSERT INTO normalization_transition
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                next_version,
                event_type,
                occurred_at.isoformat(),
                actor.identity.display_name,
                dry_run.to_json(),
            ],
        )
