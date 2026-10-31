"""Publish bounded observations with comparison and pin approval with a journal.

All helpers reuse their caller's workspace-engine transaction. Reassessment
reads one immutable observation and current intent, never rows or Odoo data.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import duckdb

from impodo.application.workspace.execution.business_review import (
    BusinessIntentEvidencePage,
    BusinessIntentReview,
    build_business_intent_review,
    business_intent_evidence_page,
)
from impodo.application.workspace.execution.navigation import ExecutionReviewProjection
from impodo.domain.execution.models import ExecutionRun
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.business_intent import BusinessIntentRevision
from impodo.domain.workspace.business_intent_assessment import (
    BusinessIntentAssessment,
    BusinessIntentAssessmentSummary,
    BusinessWriteObservation,
    assess_business_intent,
)
from impodo.domain.workspace.errors import WorkspaceError

from .repository import DuckDbRepository


def _latest_intent(
    connection: duckdb.DuckDBPyConnection,
) -> BusinessIntentRevision | None:
    row = connection.execute(
        "SELECT revision_json FROM business_intent_revision ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return BusinessIntentRevision.from_json(str(row[0])) if row else None


def publish_business_observation(
    connection: duckdb.DuckDBPyConnection,
    observation: BusinessWriteObservation,
    *, publish_review: bool = True,
) -> BusinessIntentAssessment:
    """Publish aggregate evidence and assessment in the comparison transaction."""
    revision = _latest_intent(connection)
    assessment = assess_business_intent(observation, revision)
    connection.execute(
        "INSERT INTO business_write_observation VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        [observation.content_hash, observation.snapshot_hash, observation.to_json()],
    )
    connection.execute(
        "INSERT INTO business_intent_assessment VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        [
            assessment.content_hash,
            assessment.snapshot_hash,
            assessment.intent_hash,
            assessment.to_json(),
        ],
    )
    connection.execute(
        """
        INSERT INTO business_intent_current VALUES (1, ?, ?, ?)
        ON CONFLICT (singleton_id) DO UPDATE SET snapshot_hash = excluded.snapshot_hash,
            observation_hash = excluded.observation_hash, assessment_hash = excluded.assessment_hash
    """,
        [observation.snapshot_hash, observation.content_hash, assessment.content_hash],
    )
    if publish_review:
        row = connection.execute("SELECT review_json FROM preflight_execution_projection WHERE snapshot_hash = ?", [observation.snapshot_hash]).fetchone()
        if row:
            projection = ExecutionReviewProjection.from_json(str(row[0]))
            labels = {item.target_model: item.dataset for item in projection.datasets}
            review = build_business_intent_review(observation, revision, assessment, labels=labels)
            projection = projection.with_business_intent(review)
            connection.execute("UPDATE preflight_execution_projection SET review_json = ? WHERE run_id = ? AND snapshot_hash = ?",
                [projection.to_json(), observation.preflight_run_id, observation.snapshot_hash])
    return assessment


def invalidate_business_review(connection: duckdb.DuckDBPyConnection) -> None:
    """Retire only the future review candidate; approved journals keep their view."""
    row = connection.execute("""
        SELECT projection.run_id, projection.review_json FROM preflight_execution_projection AS projection
        JOIN preflight_current AS current ON current.run_id = projection.run_id
        WHERE current.singleton_id = 1
    """).fetchone()
    if row and str(row[1] or ""):
        projection = ExecutionReviewProjection.from_json(str(row[1]))
        if projection.business_intent:
            projection = projection.with_business_intent(projection.business_intent.stale())
            connection.execute("UPDATE preflight_execution_projection SET review_json = ? WHERE run_id = ?", [projection.to_json(), row[0]])


def _business_projection(connection: duckdb.DuckDBPyConnection, snapshot_hash: str, run_id: str) -> ExecutionReviewProjection | None:
    if run_id:
        row = connection.execute("""
            SELECT json_extract_string(approval.approval_json, '$.review_json')
            FROM business_intent_approval AS approval JOIN execution_run AS run USING (run_id)
            WHERE run_id = ? AND run.snapshot_hash = ?
        """, [run_id, snapshot_hash]).fetchone()
    else:
        row = connection.execute("""
            SELECT projection.review_json FROM preflight_execution_projection AS projection
            JOIN preflight_current AS current ON current.run_id = projection.run_id
            WHERE current.singleton_id = 1 AND projection.snapshot_hash = ?
        """, [snapshot_hash]).fetchone()
    return ExecutionReviewProjection.from_json(str(row[0])) if row and row[0] else None


def current_business_assessment(
    connection: duckdb.DuckDBPyConnection,
    workspace_id: str,
    snapshot_hash: str,
) -> BusinessIntentAssessment | None:
    """Read one current sidecar, rejecting a changed intent without decoding it."""
    row = connection.execute(
        """
        SELECT assessment.assessment_json, assessment.content_hash,
               current.observation_hash, assessment.intent_hash,
               projection.target_hash, projection.run_id
          FROM business_intent_current AS current
          JOIN business_intent_assessment AS assessment ON assessment.content_hash = current.assessment_hash
          JOIN preflight_execution_projection AS projection ON projection.snapshot_hash = current.snapshot_hash
          JOIN preflight_current AS preflight ON preflight.run_id = projection.run_id
         WHERE current.singleton_id = 1 AND current.snapshot_hash = ?
           AND assessment.intent_hash IS NOT DISTINCT FROM (
               SELECT content_hash FROM business_intent_revision ORDER BY version DESC LIMIT 1)
    """,
        [snapshot_hash],
    ).fetchone()
    if row is None:
        return None
    assessment = BusinessIntentAssessment.from_json(str(row[0]))
    if (
        assessment.workspace_id != workspace_id
        or assessment.snapshot_hash != snapshot_hash
        or assessment.content_hash != str(row[1])
        or assessment.observation_hash != str(row[2])
        or assessment.intent_hash != row[3]
        or assessment.target_hash != str(row[4])
        or assessment.preflight_run_id != str(row[5])
    ):
        raise WorkspaceError("Business intent assessment belongs to different evidence")
    return assessment


def record_business_intent_approval(
    connection: duckdb.DuckDBPyConnection,
    workspace_id: str,
    run: ExecutionRun,
    *,
    expected_hash: str,
    actor: Actor,
) -> None:
    """Require the exact confirmed current assessment before inserting a journal."""
    assessment = current_business_assessment(
        connection, workspace_id, run.snapshot_hash
    )
    if (
        assessment is None
        or not assessment.ready
        or assessment.content_hash != expected_hash
        or assessment.preflight_run_id != run.preflight_run_id
        or assessment.target_hash != run.target_hash
    ):
        raise WorkspaceError(
            "Review current business expectations before starting this load"
        )
    counts = connection.execute(
        "SELECT create_count + update_count, review_json FROM preflight_execution_projection WHERE run_id = ? AND snapshot_hash = ?",
        [run.preflight_run_id, run.snapshot_hash],
    ).fetchone()
    if counts is None or int(counts[0]) != len(run.rows):
        raise WorkspaceError("The journal rows differ from the assessed snapshot")
    try:
        connection.execute(
            "UPDATE business_intent_current SET assessment_hash = ? WHERE singleton_id = 1 AND assessment_hash = ?",
            [assessment.content_hash, assessment.content_hash],
        )
    except duckdb.TransactionException as error:
        raise WorkspaceError(
            "Business expectations changed while approving this load"
        ) from error
    approval = {
        "run_id": run.run_id,
        "assessment_hash": assessment.content_hash,
        "intent_hash": assessment.intent_hash,
        "snapshot_hash": run.snapshot_hash,
        "actor": asdict(actor.identity),
        "approved_at": run.started_at.isoformat(),
    }
    raw_review = json.loads(str(counts[1]))
    if raw_review.get("business_intent"):
        projection = ExecutionReviewProjection.from_json(str(counts[1]))
        if projection.summary.business_intent_assessment_hash != assessment.content_hash or not projection.business_intent or not projection.business_intent.ready:
            raise WorkspaceError("The business expectations review changed before approval")
        approval["review_json"] = projection.to_json()
    connection.execute(
        "INSERT INTO business_intent_approval VALUES (?, ?, ?)",
        [
            run.run_id,
            assessment.content_hash,
            canonical_json({**approval, "content_hash": content_hash(approval)}),
        ],
    )


class BusinessIntentAssessmentRepository(DuckDbRepository):
    """Supply bounded sidecar queries and explicit local reassessment."""

    def get_business_intent_review(self, workspace_id: str, snapshot_hash: str, *, run_id: str = "") -> BusinessIntentReview | None:
        """Read the committed compact view, including an original journal approval."""
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            projection = _business_projection(connection, snapshot_hash, run_id)
        return projection.business_intent if projection else None

    def get_business_intent_evidence(
        self, workspace_id: str, snapshot_hash: str, *, expected_assessment_hash: str,
        model: str, page: int, run_id: str = "",
    ) -> BusinessIntentEvidencePage:
        """Read exact aggregate metadata once, returning only one bounded field page."""
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            projection = _business_projection(connection, snapshot_hash, run_id)
            approved_by = ""
            if run_id:
                row = connection.execute("""
                    SELECT assessment.assessment_json, approval.approval_json,
                           run.preflight_run_id, run.target_hash
                    FROM business_intent_approval AS approval
                    JOIN execution_run AS run USING (run_id)
                    JOIN business_intent_assessment AS assessment ON assessment.content_hash = approval.assessment_hash
                    WHERE run_id = ? AND run.snapshot_hash = ?
                """, [run_id, snapshot_hash]).fetchone()
                if row is None:
                    raise WorkspaceError("This saved load has no detailed business expectation receipt")
                assessment = BusinessIntentAssessment.from_json(str(row[0]))
                approval = json.loads(str(row[1]))
                expected_hash = approval.pop("content_hash", None)
                if content_hash(approval) != expected_hash or approval.get("assessment_hash") != assessment.content_hash or approval.get("snapshot_hash") != snapshot_hash or approval.get("run_id") != run_id or approval.get("intent_hash") != assessment.intent_hash or not assessment.ready or str(row[2]) != assessment.preflight_run_id or str(row[3]) != assessment.target_hash:
                    raise WorkspaceError("The original approval receipt is invalid")
                approved_by = str(approval["actor"]["display_name"])
            else:
                assessment = current_business_assessment(connection, workspace_id, snapshot_hash)
            if assessment is None or assessment.content_hash != expected_assessment_hash:
                raise WorkspaceError("The expectations review changed. Open Final review again.")
            observed_row = connection.execute("SELECT observation_json FROM business_write_observation WHERE content_hash = ?", [assessment.observation_hash]).fetchone()
            intent_row = connection.execute("SELECT revision_json FROM business_intent_revision WHERE content_hash = ?", [assessment.intent_hash]).fetchone() if assessment.intent_hash else None
        if observed_row is None:
            raise WorkspaceError("The recorded business effects are missing")
        observation = BusinessWriteObservation.from_json(str(observed_row[0]))
        revision = BusinessIntentRevision.from_json(str(intent_row[0])) if intent_row else None
        if observation.workspace_id != workspace_id or assessment.workspace_id != workspace_id or observation.snapshot_hash != snapshot_hash or observation.content_hash != assessment.observation_hash or (revision is not None and (revision.content_hash != assessment.intent_hash or revision.workspace_id != workspace_id)):
            raise WorkspaceError("The business expectation evidence belongs to another comparison")
        labels = {item.target_model: item.dataset for item in projection.datasets} if projection else {}
        return business_intent_evidence_page(observation, revision, assessment, model=model, page=page,
            labels=labels, approved_by=approved_by, run_id=run_id)

    def get_business_intent_assessment(
        self, workspace_id: str, snapshot_hash: str
    ) -> BusinessIntentAssessmentSummary | None:
        """Fetch bounded readiness without transferring the full deviation list."""
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT assessment.content_hash,
                       assessment.intent_hash IS NOT NULL AND json_array_length(json_extract(assessment.assessment_json, '$.deviations')) = 0,
                       json_extract_string(assessment.assessment_json, '$.deviations[0]'),
                       json_extract_string(assessment.assessment_json, '$.workspace_id'),
                       json_extract_string(assessment.assessment_json, '$.snapshot_hash'),
                       json_extract_string(assessment.assessment_json, '$.content_hash')
                  FROM business_intent_current AS current
                  JOIN business_intent_assessment AS assessment ON assessment.content_hash = current.assessment_hash
                  JOIN preflight_execution_projection AS projection ON projection.snapshot_hash = current.snapshot_hash
                  JOIN preflight_current AS preflight ON preflight.run_id = projection.run_id
                 WHERE current.singleton_id = 1 AND current.snapshot_hash = ?
                   AND assessment.intent_hash IS NOT DISTINCT FROM (
                       SELECT content_hash FROM business_intent_revision ORDER BY version DESC LIMIT 1)
            """,
                [snapshot_hash],
            ).fetchone()
        if row is None:
            return None
        if (
            str(row[3]) != workspace_id
            or str(row[4]) != snapshot_hash
            or str(row[5]) != str(row[0])
        ):
            raise WorkspaceError(
                "Business intent summary belongs to different evidence"
            )
        return BusinessIntentAssessmentSummary(
            str(row[0]), bool(row[1]), (str(row[2]),) if row[2] else ()
        )

    def get_business_write_observation(
        self, workspace_id: str, snapshot_hash: str
    ) -> BusinessWriteObservation:
        rows = self._read_json_rows(
            workspace_id,
            """
            SELECT observation_json FROM business_write_observation AS observation
            JOIN business_intent_current AS current ON current.observation_hash = observation.content_hash
            WHERE current.singleton_id = 1 AND current.snapshot_hash = ?
        """,
            [snapshot_hash],
        )
        if not rows:
            raise WorkspaceError("Compare with Odoo again to record business effects")
        result = BusinessWriteObservation.from_json(rows[0])
        if result.workspace_id != workspace_id or result.snapshot_hash != snapshot_hash:
            raise WorkspaceError("Business effects belong to another comparison")
        return result

    def reassess_business_intent(
        self, workspace_id: str, snapshot_hash: str, *, actor: Actor
    ) -> BusinessIntentAssessment:
        self._assert_workspace_mutable(workspace_id)
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT observation.observation_json FROM business_intent_current AS current
                    JOIN business_write_observation AS observation ON observation.content_hash = current.observation_hash
                    JOIN preflight_execution_projection AS projection ON projection.snapshot_hash = current.snapshot_hash
                    JOIN preflight_current AS preflight ON preflight.run_id = projection.run_id
                    WHERE current.singleton_id = 1 AND current.snapshot_hash = ?
                """,
                    [snapshot_hash],
                ).fetchone()
                if row is None:
                    raise WorkspaceError(
                        "The comparison changed before reassessing business expectations"
                    )
                observation = BusinessWriteObservation.from_json(str(row[0]))
                if observation.workspace_id != workspace_id:
                    raise WorkspaceError("Business effects belong to another workspace")
                assessment = publish_business_observation(connection, observation)
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="BUSINESS_INTENT_ASSESSED",
                    detail=f"assessment {assessment.content_hash}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return assessment

    def require_business_intent_approval(
        self, workspace_id: str, run: ExecutionRun
    ) -> None:
        """Validate original approval, ignoring intent edits after journal start."""
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            rows = connection.execute(
                """
            SELECT approval.assessment_hash, assessment.assessment_json, approval.approval_json
              FROM business_intent_approval AS approval
              JOIN execution_run AS run ON run.run_id = approval.run_id
              LEFT JOIN business_intent_assessment AS assessment ON assessment.content_hash = approval.assessment_hash
             WHERE approval.run_id = ? AND run.snapshot_hash = ?
            """,
                [run.run_id, run.snapshot_hash],
            ).fetchall()
        if not rows:
            raise WorkspaceError(
                "The interrupted journal has no business intent approval"
            )
        if rows[0][0] is None and rows[0][2] is None:
            return  # Only forward migration can grandfather an existing journal.
        assessment = BusinessIntentAssessment.from_json(str(rows[0][1]))
        approval = json.loads(str(rows[0][2]))
        expected_hash = approval.pop("content_hash", None)
        if (
            not assessment.ready
            or assessment.workspace_id != workspace_id
            or assessment.snapshot_hash != run.snapshot_hash
            or assessment.target_hash != run.target_hash
            or assessment.preflight_run_id != run.preflight_run_id
            or assessment.content_hash != str(rows[0][0])
            or content_hash(approval) != expected_hash
            or approval.get("assessment_hash") != assessment.content_hash
            or approval.get("intent_hash") != assessment.intent_hash
            or approval.get("snapshot_hash") != run.snapshot_hash
            or approval.get("run_id") != run.run_id
            or approval.get("approved_at") != run.started_at.isoformat()
        ):
            raise WorkspaceError(
                "The interrupted journal business intent approval changed"
            )
