"""Build and query bounded quality, lineage, and relationship indexes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...domain.staging.preparation_session import (
    PreparationSessionStatus,
)
from impodo.domain.workspace.errors import WorkspaceError
from .constants import (
    PREPARATION_SESSION_ROW_BATCH_SIZE,
)


class PreparationQualityIndex:
    def _resolve_relationship_edges(
        self,
        workspace_id: str,
        session_id: str,
    ) -> None:
        """Classify every incoming reference with one set-based parent join."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            connection.begin()
            try:
                self._require_status(
                    connection,
                    session_id,
                    PreparationSessionStatus.FINALIZING,
                )
                connection.execute(
                    """
                    WITH matches AS (
                        SELECT edge.child_ordinal, edge.target_field,
                               edge.item_ordinal,
                               COUNT(parent_identity.ordinal) AS match_count,
                               MIN(parent_identity.ordinal) AS parent_ordinal,
                               MIN(parent.disposition) AS parent_disposition
                          FROM preparation_relationship_edge AS edge
                          LEFT JOIN preparation_direct_identity AS parent_identity
                            ON parent_identity.session_id = edge.session_id
                           AND parent_identity.dataset = edge.parent_dataset
                           AND parent_identity.identity_hash =
                               edge.parent_identity_hash
                          LEFT JOIN canonical_staging_row AS parent
                            ON parent.run_id = parent_identity.session_id
                           AND parent.ordinal = parent_identity.ordinal
                         WHERE edge.session_id = ?
                         GROUP BY edge.child_ordinal, edge.target_field,
                                  edge.item_ordinal
                    )
                    UPDATE preparation_relationship_edge AS edge
                       SET match_count = matches.match_count,
                           match_state = CASE
                               WHEN matches.match_count = 0 THEN 'MISSING'
                               WHEN matches.match_count = 1 THEN 'UNIQUE'
                               ELSE 'DUPLICATE'
                           END,
                           resolution_state = CASE
                               WHEN matches.match_count = 0 THEN 'MISSING'
                               WHEN matches.match_count > 1 THEN 'AMBIGUOUS'
                               WHEN matches.parent_disposition IN
                                    ('CANDIDATE', 'REFERENCE')
                                   THEN 'RESOLVED'
                               ELSE 'UNSAFE_PARENT'
                           END,
                           resolved_parent_ordinal = CASE
                               WHEN matches.match_count = 1
                                   THEN matches.parent_ordinal
                               ELSE NULL
                           END
                      FROM matches
                     WHERE edge.session_id = ?
                       AND edge.child_ordinal = matches.child_ordinal
                       AND edge.target_field = matches.target_field
                       AND edge.item_ordinal = matches.item_ordinal
                    """,
                    [session_id, session_id],
                )
                invalid = connection.execute(
                    """
                    SELECT 1
                      FROM preparation_relationship_edge AS edge
                      LEFT JOIN canonical_staging_row AS child
                        ON child.run_id = edge.session_id
                       AND child.ordinal = edge.child_ordinal
                     WHERE edge.session_id = ?
                       AND (
                           child.row_id IS NULL
                           OR edge.match_state = 'PENDING'
                           OR edge.resolution_state = 'PENDING'
                           OR (edge.match_state = 'UNIQUE') !=
                              (edge.resolved_parent_ordinal IS NOT NULL)
                       )
                     LIMIT 1
                    """,
                    [session_id],
                ).fetchone()
                if invalid is not None:
                    raise WorkspaceError("Prepared relationship facts are incomplete")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def physical_rows(
        self,
        workspace_id: str,
        session_id: str,
    ) -> dict[str, tuple[int, ...]]:
        """Load compact source-row coordinates required by current quality APIs."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            cursor = connection.execute(
                """
                SELECT physical_dataset_id, source_row
                  FROM preparation_physical_row
                 WHERE session_id = ?
                 ORDER BY physical_dataset_id, source_row
                """,
                [self._session_id(session_id)],
            )
            grouped: dict[str, list[int]] = {}
            while batch := cursor.fetchmany(PREPARATION_SESSION_ROW_BATCH_SIZE):
                for physical_dataset_id, source_row in batch:
                    grouped.setdefault(str(physical_dataset_id), []).append(
                        int(source_row)
                    )
        return {dataset_id: tuple(rows) for dataset_id, rows in grouped.items()}

    def _bounded_quality_index(
        self,
        workspace_id: str,
        session_id: str,
        physical_rows: Mapping[str, Sequence[int]],
    ) -> dict[str, object] | None:
        """Validate a direct prepared run using set-based narrow relations."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            header = connection.execute(
                """
                SELECT COUNT(*), COUNT(*) FILTER (WHERE row_json = ''),
                       COUNT(DISTINCT row_id),
                       COUNT(*) FILTER (WHERE disposition = 'CANDIDATE'),
                       COUNT(*) FILTER (WHERE disposition = 'REFERENCE'),
                       COUNT(*) FILTER (WHERE disposition = 'BLOCKED'),
                       COUNT(*) FILTER (WHERE disposition = 'QUARANTINED'),
                       COUNT(*) FILTER (WHERE disposition = 'EXCLUDED')
                  FROM canonical_staging_row
                 WHERE run_id = ?
                """,
                [session_id],
            ).fetchone()
            if header is None:
                return None
            row_count = int(header[0])
            if (
                row_count == 0
                or int(header[1]) not in {0, row_count}
                or int(header[2]) != row_count
            ):
                return None
            invalid_order = connection.execute(
                """
                SELECT 1
                  FROM (
                    SELECT ordinal, dataset, source_row, row_id,
                           LAG(ordinal) OVER (ORDER BY ordinal) AS prior_ordinal,
                           LAG(dataset) OVER (ORDER BY ordinal) AS prior_dataset,
                           LAG(source_row) OVER (ORDER BY ordinal) AS prior_source,
                           LAG(row_id) OVER (ORDER BY ordinal) AS prior_row_id
                      FROM canonical_staging_row
                     WHERE run_id = ?
                  ) AS ordered
                 WHERE ordinal != COALESCE(prior_ordinal + 1, 0)
                    OR (prior_ordinal IS NOT NULL AND
                        (dataset, source_row, row_id) <
                        (prior_dataset, prior_source, prior_row_id))
                 LIMIT 1
                """,
                [session_id],
            ).fetchone()
            if invalid_order is not None:
                return None
            lineage_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM preparation_physical_row
                      WHERE session_id = ?),
                    (SELECT COUNT(*) FROM preparation_lineage
                      WHERE session_id = ?),
                    (SELECT COUNT(*)
                       FROM canonical_staging_row AS row
                       JOIN preparation_lineage AS lineage
                         ON lineage.session_id = row.run_id
                        AND lineage.dataset = row.dataset
                        AND lineage.output_source_row = row.source_row
                      WHERE row.run_id = ?),
                    (SELECT COUNT(*) FROM (
                         SELECT dataset, output_source_row
                           FROM preparation_lineage WHERE session_id = ?
                          GROUP BY dataset, output_source_row
                         HAVING COUNT(*) != 1)),
                    (SELECT COUNT(*) FROM (
                         SELECT physical_dataset_id, physical_source_row
                           FROM preparation_lineage WHERE session_id = ?
                          GROUP BY physical_dataset_id, physical_source_row
                         HAVING COUNT(*) != 1))
                """,
                [session_id, session_id, session_id, session_id, session_id],
            ).fetchone()
            expected_physical = sum(len(rows) for rows in physical_rows.values())
            stored_datasets = {
                str(item[0]): int(item[1])
                for item in connection.execute(
                    """
                    SELECT physical_dataset_id, COUNT(*)
                      FROM preparation_physical_row
                     WHERE session_id = ?
                     GROUP BY physical_dataset_id
                    """,
                    [session_id],
                ).fetchall()
            }
            expected_datasets = {
                dataset_id: len(rows) for dataset_id, rows in physical_rows.items()
            }
            if (
                lineage_counts is None
                or expected_physical != row_count
                or stored_datasets != expected_datasets
                or any(int(value) != row_count for value in lineage_counts[:3])
                or int(lineage_counts[3])
                or int(lineage_counts[4])
            ):
                return None
            issue_rows = connection.execute(
                """
                SELECT issue.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, issue.issue_json,
                       lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row_issue AS issue
                  JOIN canonical_staging_row AS row
                   ON row.run_id = issue.run_id
                   AND row.ordinal = issue.ordinal
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE issue.run_id = ?
                 ORDER BY issue.ordinal, issue.issue_ordinal
                """,
                [session_id],
            ).fetchall()
            collisions = connection.execute(
                """
                WITH collision AS (
                    SELECT quality_identity_key, COUNT(*) AS identity_count
                      FROM canonical_staging_row
                     WHERE run_id = ? AND quality_identity_key IS NOT NULL
                     GROUP BY quality_identity_key
                    HAVING COUNT(*) > 1
                )
                SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, collision.identity_count,
                       lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row AS row
                  JOIN collision USING (quality_identity_key)
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE row.run_id = ?
                 ORDER BY row.ordinal
                """,
                [session_id, session_id],
            ).fetchall()
            exception_rows = connection.execute(
                """
                SELECT row.ordinal, row.row_id, row.dataset, row.source_row,
                       row.disposition, lineage.physical_dataset_id,
                       lineage.physical_source_row
                  FROM canonical_staging_row AS row
                  JOIN preparation_lineage AS lineage
                    ON lineage.session_id = row.run_id
                   AND lineage.dataset = row.dataset
                   AND lineage.output_source_row = row.source_row
                 WHERE row.run_id = ?
                   AND row.disposition NOT IN ('CANDIDATE', 'REFERENCE')
                 ORDER BY row.ordinal
                """,
                [session_id],
            ).fetchall()
            return {
                "row_count": row_count,
                "disposition_counts": {
                    "CANDIDATE": int(header[3]),
                    "REFERENCE": int(header[4]),
                    "BLOCKED": int(header[5]),
                    "QUARANTINED": int(header[6]),
                    "EXCLUDED": int(header[7]),
                },
                "issue_rows": tuple(issue_rows),
                "collisions": tuple(collisions),
                "exception_rows": tuple(exception_rows),
            }

    def _bounded_relationship_findings(
        self,
        workspace_id: str,
        session_id: str,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
    ) -> tuple[tuple[object, ...], ...]:
        """Return only affected children after one recursive set operation."""

        canonical_session_id = self._session_id(session_id)
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            relationship_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM preparation_relationship_edge
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                ).fetchone()[0]
            )
            if relationship_count == 0:
                return ()
            connection.begin()
            try:
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_initial_unsafe (
                        row_id VARCHAR PRIMARY KEY
                    )
                    """
                )
                if unsafe_row_ids:
                    connection.execute(
                        """
                        INSERT INTO relationship_initial_unsafe
                        SELECT DISTINCT CAST(unnest(?) AS VARCHAR)
                        """,
                        [list(unsafe_row_ids)],
                    )
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_propagating_dataset (
                        dataset VARCHAR PRIMARY KEY
                    )
                    """
                )
                if propagating_datasets:
                    connection.execute(
                        """
                        INSERT INTO relationship_propagating_dataset
                        SELECT DISTINCT CAST(unnest(?) AS VARCHAR)
                        """,
                        [list(propagating_datasets)],
                    )
                connection.execute(
                    """
                    UPDATE preparation_relationship_edge
                       SET resolution_state = CASE
                           WHEN match_state = 'MISSING' THEN 'MISSING'
                           WHEN match_state = 'DUPLICATE' THEN 'AMBIGUOUS'
                           WHEN match_state = 'UNIQUE' THEN 'RESOLVED'
                           ELSE 'PENDING'
                       END
                     WHERE session_id = ?
                    """,
                    [canonical_session_id],
                )
                connection.execute(
                    """
                    CREATE TEMP TABLE relationship_unsafe AS
                    WITH RECURSIVE unsafe(ordinal) AS (
                        SELECT row.ordinal
                          FROM canonical_staging_row AS row
                          JOIN relationship_initial_unsafe AS initial
                            ON initial.row_id = row.row_id
                         WHERE row.run_id = ?
                        UNION
                        SELECT edge.child_ordinal
                          FROM preparation_relationship_edge AS edge
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                          JOIN relationship_propagating_dataset AS allowed
                            ON allowed.dataset = child.dataset
                         WHERE edge.session_id = ?
                           AND edge.resolution_state IN ('MISSING', 'AMBIGUOUS')
                        UNION
                        SELECT edge.child_ordinal
                          FROM preparation_relationship_edge AS edge
                          JOIN unsafe AS parent
                            ON parent.ordinal = edge.resolved_parent_ordinal
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                         WHERE edge.session_id = ?
                           AND edge.match_state = 'UNIQUE'
                           AND child.dataset IN (
                               SELECT dataset
                                 FROM relationship_propagating_dataset
                           )
                    )
                    SELECT DISTINCT ordinal FROM unsafe
                    """,
                    [
                        canonical_session_id,
                        canonical_session_id,
                        canonical_session_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE preparation_relationship_edge AS edge
                       SET resolution_state = CASE
                           WHEN edge.match_state = 'MISSING' THEN 'MISSING'
                           WHEN edge.match_state = 'DUPLICATE' THEN 'AMBIGUOUS'
                           WHEN parent.ordinal IS NOT NULL THEN 'UNSAFE_PARENT'
                           ELSE 'RESOLVED'
                       END
                      FROM (
                          SELECT ordinal FROM relationship_unsafe
                      ) AS parent
                     WHERE edge.session_id = ?
                       AND edge.match_state = 'UNIQUE'
                       AND edge.resolved_parent_ordinal = parent.ordinal
                    """,
                    [canonical_session_id],
                )
                findings = tuple(
                    connection.execute(
                        """
                        SELECT child.ordinal, child.row_id, child.dataset,
                               child.source_row, child.disposition,
                               lineage.physical_dataset_id,
                               lineage.physical_source_row,
                               MIN(edge.resolution_state) AS resolution_state
                          FROM preparation_relationship_edge AS edge
                          JOIN canonical_staging_row AS child
                            ON child.run_id = edge.session_id
                           AND child.ordinal = edge.child_ordinal
                          JOIN preparation_lineage AS lineage
                            ON lineage.session_id = child.run_id
                           AND lineage.dataset = child.dataset
                           AND lineage.output_source_row = child.source_row
                         WHERE edge.session_id = ?
                           AND edge.resolution_state != 'RESOLVED'
                         GROUP BY child.ordinal, child.row_id, child.dataset,
                                  child.source_row, child.disposition,
                                  lineage.physical_dataset_id,
                                  lineage.physical_source_row
                         ORDER BY child.ordinal
                        """,
                        [canonical_session_id],
                    ).fetchall()
                )
                connection.commit()
                return findings
            except Exception:
                connection.rollback()
                raise

    def _iter_quality_index_batches(
        self,
        workspace_id: str,
        session_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        if connection is None:
            database_path = (
                self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            )
            with self._connect(database_path) as owned:
                yield from self._iter_quality_index_batches(
                    workspace_id,
                    session_id,
                    batch_size=batch_size,
                    connection=owned,
                )
            return
        next_ordinal = 0
        while batch := connection.execute(
            """
            SELECT ordinal, row_id, dataset, source_row, record_label,
                   disposition
              FROM canonical_staging_row
             WHERE run_id = ? AND ordinal >= ?
             ORDER BY ordinal LIMIT ?
            """,
            [session_id, next_ordinal, batch_size],
        ).fetchall():
            yield batch
            next_ordinal += len(batch)

    def _iter_accounting_index_batches(
        self,
        workspace_id: str,
        session_id: str,
        *,
        batch_size: int,
        connection=None,
    ):
        if connection is None:
            database_path = (
                self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
            )
            with self._connect(database_path) as owned:
                yield from self._iter_accounting_index_batches(
                    workspace_id,
                    session_id,
                    batch_size=batch_size,
                    connection=owned,
                )
            return
        offset = 0
        while batch := connection.execute(
            """
            SELECT lineage.physical_dataset_id,
                   lineage.physical_source_row, row.row_id
              FROM preparation_lineage AS lineage
              JOIN canonical_staging_row AS row
                ON row.run_id = lineage.session_id
               AND row.dataset = lineage.dataset
               AND row.source_row = lineage.output_source_row
             WHERE lineage.session_id = ?
             ORDER BY lineage.physical_dataset_id,
                      lineage.physical_source_row
             LIMIT ? OFFSET ?
            """,
            [session_id, batch_size, offset],
        ).fetchall():
            yield batch
            offset += len(batch)

    def _direct_index_contains_row_id(
        self,
        workspace_id: str,
        session_id: str,
        row_id: str,
    ) -> bool:
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        with self._connect(database_path) as connection:
            return (
                connection.execute(
                    """
                SELECT 1 FROM canonical_staging_row
                 WHERE run_id = ? AND row_id = ? LIMIT 1
                """,
                    [session_id, row_id],
                ).fetchone()
                is not None
            )
