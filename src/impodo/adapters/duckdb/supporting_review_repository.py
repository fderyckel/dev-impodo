"""Persist complete supporting decisions in the workspace data plane only."""

from __future__ import annotations

from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_review import (
    SupportingReview,
    SupportingReviewSummary,
)

from .repository import DuckDbRepository


class SupportingReviewRecords(DuckDbRepository):
    """Use one transaction for optimistic publication, retry lookup and audit."""

    def get_supporting_review_summary(
        self, workspace_id: str
    ) -> SupportingReviewSummary | None:
        """Read bounded metadata without selecting the complete decision JSON."""
        value = self._read_singleton_json(
            workspace_id,
            "SELECT summary_json FROM supporting_review_projection WHERE singleton_id = TRUE",
        )
        result = SupportingReviewSummary.from_json(value) if value else None
        if result and result.workspace_id != workspace_id:
            raise WorkspaceError("Supporting summary belongs to another workspace")
        return result

    def rebuild_supporting_review_summary(
        self, workspace_id: str
    ) -> SupportingReviewSummary | None:
        """Recover a missing projection from committed evidence without Odoo I/O."""
        self._assert_workspace_mutable(workspace_id)
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    "SELECT review_json FROM supporting_data_review ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                review = SupportingReview.from_json(str(row[0]))
                if review.workspace_id != workspace_id:
                    raise WorkspaceError(
                        "Supporting summary belongs to another workspace"
                    )
                summary = SupportingReviewSummary.from_review(review)
                connection.execute(
                    "INSERT OR REPLACE INTO supporting_review_projection VALUES (TRUE, ?)",
                    [summary.to_json()],
                )
                connection.commit()
                return summary
            except Exception:
                connection.rollback()
                raise

    def get_supporting_review(self, workspace_id: str) -> SupportingReview | None:
        """Read one immutable current revision without loading its history."""
        value = self._read_singleton_json(
            workspace_id,
            "SELECT review_json FROM supporting_data_review ORDER BY version DESC LIMIT 1",
        )
        review = SupportingReview.from_json(value) if value else None
        if review is not None and review.workspace_id != workspace_id:
            raise WorkspaceError(
                "Stored supporting choices belong to another workspace"
            )
        return review

    def save_supporting_review(
        self,
        workspace_id: str,
        review: SupportingReview,
        *,
        expected_version: int,
        actor: Actor,
    ) -> SupportingReview:
        """Append the next exact revision or return the same actor's exact retry."""
        self._assert_workspace_mutable(workspace_id)
        if (review.actor_issuer, review.actor_subject, review.actor_display_name) != (
            actor.identity.issuer,
            actor.identity.subject_id,
            actor.identity.display_name,
        ):
            raise WorkspaceError(
                "Supporting review author does not match the verified Actor"
            )
        if (
            review.workspace_id != workspace_id
            or review.version != expected_version + 1
        ):
            raise WorkspaceError(
                "Supporting review belongs to another revision or workspace"
            )
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                prior = connection.execute(
                    "SELECT review_json FROM supporting_data_review WHERE operation_id = ?",
                    [review.operation_id],
                ).fetchone()
                if prior:
                    existing = SupportingReview.from_json(str(prior[0]))
                    if (
                        existing.request_hash != review.request_hash
                        or existing.actor_issuer != actor.identity.issuer
                        or existing.actor_subject != actor.identity.subject_id
                    ):
                        raise WorkspaceError(
                            "This supporting-review operation was already used for another decision"
                        )
                    connection.rollback()
                    return existing
                actual = connection.execute(
                    "SELECT version, request_hash FROM supporting_data_review ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if (int(actual[0]) if actual else 0) != expected_version:
                    raise WorkspaceError(
                        "Supporting choices changed in another request; review them again"
                    )
                connection.execute(
                    "INSERT INTO supporting_data_review VALUES (?, ?, ?, ?)",
                    [
                        review.version,
                        review.operation_id,
                        review.request_hash,
                        review.to_json(),
                    ],
                )
                summary = SupportingReviewSummary.from_review(review)
                connection.execute(
                    "INSERT OR REPLACE INTO supporting_review_projection VALUES (TRUE, ?)",
                    [summary.to_json()],
                )
                if actual is None or str(actual[1]) != review.request_hash:
                    connection.execute("DELETE FROM mapping_current")
                    self._invalidate_canonical_staging(
                        connection, reason="SUPPORTING_CHOICES_CHANGED"
                    )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="SUPPORTING_DATA_REVIEWED",
                    detail=f"revision {review.version}: {len(review.choices)} relationships",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return review
