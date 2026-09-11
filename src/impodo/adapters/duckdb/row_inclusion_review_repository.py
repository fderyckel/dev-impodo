"""DuckDB persistence for protected Match-data row-inclusion reviews."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import duckdb

from impodo.domain.mapping.row_inclusion_review import (
    MAX_ROW_INCLUSION_REVIEW_PAGE_SIZE,
    RowInclusionReviewConfirmation,
    RowInclusionReviewFilter,
    RowInclusionReviewIdentity,
    RowInclusionReviewOutcome,
    RowInclusionReviewPage,
    RowInclusionReviewReport,
    RowInclusionReviewRow,
    RowInclusionReviewSnapshot,
    RowInclusionSourceValue,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError

from .repository import DuckDbRepository


class RowInclusionReviewRepository(DuckDbRepository):
    """Store immutable checks, paged decisions, and exact confirmations."""

    def replace_current_review(
        self,
        workspace_id: str,
        report: RowInclusionReviewReport,
        *,
        actor: Actor,
    ) -> RowInclusionReviewSnapshot:
        """Publish one complete check before making it current."""

        self._assert_workspace_mutable(workspace_id)
        with self._row_inclusion_review_lock:
            return self._replace_current_review_locked(
                workspace_id,
                report,
                actor=actor,
                retry_duplicate=True,
            )

    def _replace_current_review_locked(
        self,
        workspace_id: str,
        report: RowInclusionReviewReport,
        *,
        actor: Actor,
        retry_duplicate: bool,
    ) -> RowInclusionReviewSnapshot:
        """Publish once, then recover an identical cross-process race."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        checked_at = datetime.now(timezone.utc)
        snapshot = RowInclusionReviewSnapshot(
            identity=report.identity,
            datasets=report.datasets,
            snapshot_hash=report.content_hash,
            checked_at=checked_at,
            checked_by=actor.identity.display_name,
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            existing = connection.execute(
                """
                SELECT snapshot_hash
                  FROM mapping_row_inclusion_review
                 WHERE snapshot_hash = ?
                """,
                [snapshot.snapshot_hash],
            ).fetchone()
            connection.begin()
            try:
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO mapping_row_inclusion_review (
                            snapshot_hash, identity_hash,
                            physical_selection_hash, source_selection_hash,
                            mapping_content_hash, schema_hash,
                            derived_plan_hash, evaluator_version,
                            checked_at, checked_by, snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot.snapshot_hash,
                            report.identity.content_hash,
                            report.identity.physical_selection_hash,
                            report.identity.source_selection_hash,
                            report.identity.mapping_content_hash,
                            report.identity.schema_hash,
                            report.identity.derived_plan_hash,
                            report.identity.evaluator_version,
                            checked_at.isoformat(),
                            actor.identity.display_name,
                            snapshot.to_json(),
                        ],
                    )
                    self._insert_rows(connection, snapshot.snapshot_hash, report.rows)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO mapping_row_inclusion_current
                    VALUES (1, ?)
                    """,
                    [snapshot.snapshot_hash],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ROW_INCLUSION_REVIEW_CHECKED",
                    detail=(
                        f"{snapshot.snapshot_hash}: "
                        f"{snapshot.included_count} included, "
                        f"{snapshot.excluded_count} excluded, "
                        f"{snapshot.cannot_evaluate_count} cannot evaluate"
                    ),
                    actor=actor,
                )
                connection.commit()
            except duckdb.ConstraintException as error:
                connection.rollback()
                if not retry_duplicate or "Duplicate key" not in str(error):
                    raise
                duplicate_error = error
            except Exception:
                connection.rollback()
                raise
            else:
                duplicate_error = None
        if duplicate_error is not None:
            return self._replace_current_review_locked(
                workspace_id,
                report,
                actor=actor,
                retry_duplicate=False,
            )
        return self.get_current_review(workspace_id, report.identity) or snapshot

    def get_current_review(
        self,
        workspace_id: str,
        identity: RowInclusionReviewIdentity,
    ) -> RowInclusionReviewSnapshot | None:
        """Return current evidence only when every checked input still matches."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT review.identity_hash, review.snapshot_json
                  FROM mapping_row_inclusion_current AS current
                  JOIN mapping_row_inclusion_review AS review
                    ON review.snapshot_hash = current.snapshot_hash
                 WHERE current.singleton_id = 1
                """
            ).fetchone()
        if row is None or str(row[0]) != identity.content_hash:
            return None
        snapshot = RowInclusionReviewSnapshot.from_json(str(row[1]))
        if snapshot.identity != identity:
            return None
        return snapshot

    def get_confirmation(
        self,
        workspace_id: str,
        snapshot_hash: str,
    ) -> RowInclusionReviewConfirmation | None:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT snapshot_hash, mapping_content_hash,
                       source_selection_hash, confirmed_at, confirmed_by
                  FROM mapping_row_inclusion_confirmation
                 WHERE snapshot_hash = ?
                """,
                [snapshot_hash],
            ).fetchone()
        if row is None:
            return None
        return RowInclusionReviewConfirmation(
            snapshot_hash=str(row[0]),
            mapping_content_hash=str(row[1]),
            source_selection_hash=str(row[2]),
            confirmed_at=datetime.fromisoformat(str(row[3])),
            confirmed_by=str(row[4]),
        )

    def is_confirmed(
        self,
        workspace_id: str,
        *,
        mapping_content_hash: str,
        source_selection_hash: str,
    ) -> bool:
        """Return whether the current exact row check was explicitly confirmed."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            return False
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT review.snapshot_json,
                       confirmation.snapshot_hash
                  FROM mapping_row_inclusion_current AS current
                  JOIN mapping_row_inclusion_review AS review
                    ON review.snapshot_hash = current.snapshot_hash
                  LEFT JOIN mapping_row_inclusion_confirmation AS confirmation
                    ON confirmation.snapshot_hash = review.snapshot_hash
                 WHERE review.mapping_content_hash = ?
                   AND review.source_selection_hash = ?
                   AND (
                        confirmation.snapshot_hash IS NULL
                        OR (
                            confirmation.mapping_content_hash = ?
                            AND confirmation.source_selection_hash = ?
                        )
                   )
                """,
                [
                    mapping_content_hash,
                    source_selection_hash,
                    mapping_content_hash,
                    source_selection_hash,
                ],
            ).fetchone()
        if row is None:
            return False
        snapshot = RowInclusionReviewSnapshot.from_json(str(row[0]))
        return snapshot.excluded_count == 0 or row[1] is not None

    def confirm_current_review(
        self,
        workspace_id: str,
        snapshot: RowInclusionReviewSnapshot,
        *,
        actor: Actor,
        operation_id: str | None = None,
        working_draft_version: int | None = None,
        mapping_revision_version: int | None = None,
    ) -> RowInclusionReviewConfirmation:
        """Confirm one exact current, complete, nonempty row decision."""

        if not snapshot.confirmable:
            raise WorkspaceError(
                "Resolve rows that need attention and include at least one row "
                "before confirming."
            )
        self._assert_workspace_mutable(workspace_id)
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        confirmed_at = datetime.now(timezone.utc)
        confirmation = RowInclusionReviewConfirmation(
            snapshot_hash=snapshot.snapshot_hash,
            mapping_content_hash=snapshot.identity.mapping_content_hash,
            source_selection_hash=snapshot.identity.source_selection_hash,
            confirmed_at=confirmed_at,
            confirmed_by=actor.identity.display_name,
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    """
                    SELECT row_current.snapshot_hash,
                           review.mapping_content_hash,
                           review.source_selection_hash,
                           mapping_current.version,
                           revision.content_hash,
                           revision.source_selection_hash,
                           draft.version,
                           draft.content_hash
                      FROM mapping_row_inclusion_current AS row_current
                      JOIN mapping_row_inclusion_review AS review
                        ON review.snapshot_hash = row_current.snapshot_hash
                      LEFT JOIN mapping_current
                        ON mapping_current.singleton_id = 1
                      LEFT JOIN mapping_revision AS revision
                        ON revision.mapping_id = mapping_current.mapping_id
                       AND revision.version = mapping_current.version
                      LEFT JOIN mapping_working_draft AS draft
                        ON draft.singleton_id = 1
                     WHERE row_current.singleton_id = 1
                    """
                ).fetchone()
                if not self._confirmation_inputs_are_current(
                    current,
                    snapshot,
                    working_draft_version=working_draft_version,
                    mapping_revision_version=mapping_revision_version,
                ):
                    raise WorkspaceError(
                        "The checked rows or mapping changed. Check matches "
                        "again before confirming."
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO mapping_row_inclusion_confirmation (
                        snapshot_hash, mapping_content_hash,
                        source_selection_hash, confirmed_at, confirmed_by
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        confirmation.snapshot_hash,
                        confirmation.mapping_content_hash,
                        confirmation.source_selection_hash,
                        confirmation.confirmed_at.isoformat(),
                        confirmation.confirmed_by,
                    ],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ROW_INCLUSION_REVIEW_CONFIRMED",
                    detail=(
                        f"{snapshot.snapshot_hash}: "
                        f"{snapshot.included_count} rows to use"
                    ),
                    actor=actor,
                )
                if operation_id is not None:
                    self._commit_mapping_receipt(
                        connection,
                        operation_id,
                        working_draft_version=working_draft_version,
                        mapping_revision_version=mapping_revision_version,
                        content_identity=snapshot.snapshot_hash,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_confirmation(workspace_id, snapshot.snapshot_hash) or confirmation

    @staticmethod
    def _confirmation_inputs_are_current(
        current,
        snapshot: RowInclusionReviewSnapshot,
        *,
        working_draft_version: int | None,
        mapping_revision_version: int | None,
    ) -> bool:
        """Recheck browser versions and semantic hashes inside the write transaction."""

        if current is None:
            return False
        if (
            str(current[0]) != snapshot.snapshot_hash
            or str(current[1]) != snapshot.identity.mapping_content_hash
            or str(current[2]) != snapshot.identity.source_selection_hash
        ):
            return False
        if mapping_revision_version is not None and (
            current[3] is None
            or int(current[3]) != mapping_revision_version
            or str(current[4]) != snapshot.identity.mapping_content_hash
            or str(current[5]) != snapshot.identity.source_selection_hash
        ):
            return False
        if working_draft_version is not None and (
            current[6] is None
            or int(current[6]) != working_draft_version
            or str(current[7]) != snapshot.identity.mapping_content_hash
        ):
            return False
        return True

    def get_review_page(
        self,
        workspace_id: str,
        snapshot_hash: str,
        filters: RowInclusionReviewFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> RowInclusionReviewPage:
        """Read one bounded page without loading the complete checked result."""

        if page_size < 1 or page_size > MAX_ROW_INCLUSION_REVIEW_PAGE_SIZE:
            raise WorkspaceError("Rows-to-use page size is invalid")
        if after is not None and before is not None:
            raise WorkspaceError("Choose only one rows-to-use page direction")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            current = connection.execute(
                """
                SELECT snapshot_hash FROM mapping_row_inclusion_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if current is None or str(current[0]) != snapshot_hash:
                raise WorkspaceError("Check the current rows before reviewing them")
            where_sql, parameters = self._where(filters)
            matching = connection.execute(
                f"""
                SELECT COUNT(*) FROM mapping_row_inclusion_review_row
                 WHERE snapshot_hash = ? AND {where_sql}
                """,
                [snapshot_hash, *parameters],
            ).fetchone()
            matching_count = int(matching[0]) if matching else 0
            cursor_sql = ""
            cursor_parameters: list[object] = []
            order = "ASC"
            if after is not None:
                cursor_sql = " AND ordinal > ?"
                cursor_parameters.append(after)
            elif before is not None:
                cursor_sql = " AND ordinal < ?"
                cursor_parameters.append(before)
                order = "DESC"
            result = connection.execute(
                f"""
                SELECT ordinal, dataset_id, dataset_name, source_row,
                       values_json, outcome, rule_sentence, message
                  FROM mapping_row_inclusion_review_row
                 WHERE snapshot_hash = ? AND {where_sql}{cursor_sql}
                 ORDER BY ordinal {order}
                 LIMIT ?
                """,
                [snapshot_hash, *parameters, *cursor_parameters, page_size],
            ).fetchall()
            if order == "DESC":
                result.reverse()
            ordinals = [int(item[0]) for item in result]
            if ordinals:
                preceding = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM mapping_row_inclusion_review_row
                     WHERE snapshot_hash = ? AND {where_sql} AND ordinal < ?
                    """,
                    [snapshot_hash, *parameters, ordinals[0]],
                ).fetchone()
                start_position = int(preceding[0]) + 1 if preceding else 1
            else:
                start_position = 0
        rows = tuple(self._row_from_record(item) for item in result)
        end_position = start_position + len(rows) - 1 if rows else 0
        return RowInclusionReviewPage(
            rows=rows,
            matching_count=matching_count,
            start_position=start_position,
            end_position=end_position,
            previous_before=(ordinals[0] if ordinals and start_position > 1 else None),
            next_after=(
                ordinals[-1] if ordinals and end_position < matching_count else None
            ),
        )

    @staticmethod
    def _insert_rows(connection, snapshot_hash: str, rows) -> None:
        batch = []
        for ordinal, row in enumerate(rows):
            batch.append(
                [
                    snapshot_hash,
                    ordinal,
                    row.dataset_id,
                    row.dataset_name,
                    row.source_row,
                    canonical_json([asdict_value(item) for item in row.values]),
                    row.outcome.value,
                    row.rule_sentence,
                    row.message,
                ]
            )
            if len(batch) == 1_000:
                connection.executemany(
                    """
                    INSERT INTO mapping_row_inclusion_review_row
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                batch.clear()
        if batch:
            connection.executemany(
                """
                INSERT INTO mapping_row_inclusion_review_row
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )

    @staticmethod
    def _where(filters: RowInclusionReviewFilter) -> tuple[str, list[object]]:
        clauses = ["1 = 1"]
        parameters: list[object] = []
        if filters.dataset_id:
            clauses.append("dataset_id = ?")
            parameters.append(filters.dataset_id)
        if filters.outcome:
            clauses.append("outcome = ?")
            parameters.append(filters.outcome)
        if filters.query:
            clauses.append(
                "(contains(lower(values_json), ?) OR "
                "contains(lower(rule_sentence), ?) OR "
                "contains(lower(message), ?))"
            )
            parameters.extend([filters.query.casefold()] * 3)
        return " AND ".join(clauses), parameters

    @staticmethod
    def _row_from_record(item) -> RowInclusionReviewRow:
        values = json.loads(str(item[4]))
        return RowInclusionReviewRow(
            dataset_id=str(item[1]),
            dataset_name=str(item[2]),
            source_row=int(item[3]),
            values=tuple(RowInclusionSourceValue(**value) for value in values),
            outcome=RowInclusionReviewOutcome(str(item[5])),
            rule_sentence=str(item[6]),
            message=str(item[7]),
        )

    @staticmethod
    def _commit_mapping_receipt(
        connection,
        operation_id: str,
        *,
        working_draft_version: int | None,
        mapping_revision_version: int | None,
        content_identity: str,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE mapping_mutation_receipt
               SET state = 'COMMITTED', working_draft_version = ?,
                   mapping_revision_version = ?, content_identity = ?,
                   completed_at = ?
             WHERE operation_id = ? AND state = 'PENDING'
            RETURNING operation_id
            """,
            [
                working_draft_version,
                mapping_revision_version,
                content_identity,
                datetime.now(timezone.utc).isoformat(),
                operation_id,
            ],
        ).fetchone()
        if updated is None:
            raise WorkspaceError("The rows-to-use confirmation receipt is not pending")


def asdict_value(value: RowInclusionSourceValue) -> dict[str, str]:
    return {
        "source_column_key": value.source_column_key,
        "source_column_label": value.source_column_label,
        "value": value.value,
    }
