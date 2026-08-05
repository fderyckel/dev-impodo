"""DuckDB transformation impact repository implementation."""

from __future__ import annotations

from .constants import TRANSFORMATION_IMPACT_ROW_BATCH_SIZE

from datetime import (
    datetime,
    timezone,
)
from typing import (
    Callable,
    Iterator,
    Sequence,
)


from ...access import Actor
from ...projects import ProjectNotFoundError
from ...domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactPage,
    TransformationImpactReport,
    TransformationImpactRow,
    TransformationImpactSnapshot,
)
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository







class TransformationImpactRepository(DuckDbRepository):
    """Persistence operations for transformation impact repository."""

    def get_transformation_impact_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
    ) -> TransformationImpactSnapshot | None:
        """Return the current snapshot only when every input still matches."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute(
                """
                SELECT identity_hash, physical_selection_hash,
                       source_selection_hash, mapping_content_hash,
                       schema_hash, derived_plan_hash, contract_version,
                       evaluator_version, created_at, created_by,
                       affected_row_count, evaluated_count, changed_count, fallback_count,
                       null_count, invalid_count, provided_count,
                       unchanged_count
                  FROM transformation_impact_run
                 WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return None
        stored_identity = TransformationImpactIdentity(
            physical_selection_hash=str(row[1]),
            source_selection_hash=str(row[2]),
            mapping_content_hash=str(row[3]),
            schema_hash=str(row[4]),
            derived_plan_hash=str(row[5]) if row[5] else None,
            contract_version=int(row[6]),
            evaluator_version=int(row[7]),
        )
        if (
            stored_identity != identity
            or str(row[0]) != stored_identity.content_hash
        ):
            return None
        return self._transformation_impact_snapshot(stored_identity, row)
    def replace_transformation_impact_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        build: Callable[
            [Callable[[TransformationImpactRow], None]],
            TransformationImpactReport,
        ],
        *,
        actor: Actor,
    ) -> TransformationImpactSnapshot:
        """Build and atomically replace the bounded-browser impact source."""

        with self._transformation_impact_lock:
            current = self.get_transformation_impact_snapshot(project_id, identity)
            if current is not None:
                return current
            database_path = self.project_directory(project_id) / "project.duckdb"
            if not database_path.is_file():
                raise ProjectNotFoundError("Project not found")
            created_at = datetime.now(timezone.utc)
            with self._connect(database_path) as connection:
                self._migrate_project_database(connection)
                connection.begin()
                batch: list[list[object]] = []
                ordinal = 0

                def flush() -> None:
                    if not batch:
                        return
                    connection.executemany(
                        """
                        INSERT INTO transformation_impact_row (
                            ordinal, dataset, source_row, source_column,
                            target_field, raw_value, proposed_value, rules,
                            outcome, message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    batch.clear()

                def write_row(row: TransformationImpactRow) -> None:
                    nonlocal ordinal
                    batch.append(
                        [
                            ordinal,
                            row.dataset,
                            row.source_row,
                            row.source_column,
                            row.target_field,
                            row.raw_value,
                            row.proposed_value,
                            row.rules,
                            row.outcome,
                            row.message,
                        ]
                    )
                    ordinal += 1
                    if len(batch) >= TRANSFORMATION_IMPACT_ROW_BATCH_SIZE:
                        flush()

                try:
                    connection.execute("DELETE FROM transformation_impact_row")
                    connection.execute("DELETE FROM transformation_impact_run")
                    report = build(write_row)
                    flush()
                    if report.mapping_content_hash != identity.mapping_content_hash:
                        raise WorkspaceError(
                            "Transformation impact belongs to another mapping"
                        )
                    if ordinal != report.impact_count:
                        raise WorkspaceError(
                            "Transformation impact rows were not stored completely"
                        )
                    affected = connection.execute(
                        """
                        SELECT COUNT(*)
                          FROM (
                                SELECT DISTINCT dataset, source_row
                                  FROM transformation_impact_row
                               ) AS affected_rows
                        """
                    ).fetchone()
                    affected_row_count = int(affected[0]) if affected else 0
                    connection.execute(
                        """
                        INSERT INTO transformation_impact_run (
                            singleton_id, identity_hash,
                            physical_selection_hash, source_selection_hash,
                            mapping_content_hash, schema_hash,
                            derived_plan_hash, contract_version,
                            evaluator_version, created_at, created_by,
                            affected_row_count, evaluated_count, changed_count, fallback_count,
                            null_count, invalid_count, provided_count,
                            unchanged_count
                        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            identity.content_hash,
                            identity.physical_selection_hash,
                            identity.source_selection_hash,
                            identity.mapping_content_hash,
                            identity.schema_hash,
                            identity.derived_plan_hash,
                            identity.contract_version,
                            identity.evaluator_version,
                            created_at.isoformat(),
                            actor.identity.display_name,
                            affected_row_count,
                            report.evaluated_count,
                            report.changed_count,
                            report.fallback_count,
                            report.null_count,
                            report.invalid_count,
                            report.provided_count,
                            report.unchanged_count,
                        ],
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return TransformationImpactSnapshot(
                identity=identity,
                created_at=created_at,
                created_by=actor.identity.display_name,
                affected_row_count=affected_row_count,
                report=report,
            )
    def get_transformation_impact_page(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> TransformationImpactPage:
        """Read one server-filtered page without materializing all matches."""

        if page_size < 1 or page_size > 250:
            raise WorkspaceError("Transformation impact page size is invalid")
        if after is not None and before is not None:
            raise WorkspaceError("Choose only one transformation impact cursor")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            stored = connection.execute(
                """
                SELECT identity_hash
                  FROM transformation_impact_run
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if stored is None or str(stored[0]) != identity.content_hash:
                raise WorkspaceError("Prepare the current transformation impact first")
            where_sql, parameters = self._transformation_impact_where(filters)
            matching = connection.execute(
                f"SELECT COUNT(*) FROM transformation_impact_row {where_sql}",
                parameters,
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
                SELECT ordinal, dataset, source_row, source_column,
                       target_field, raw_value, proposed_value, rules,
                       outcome, message
                  FROM transformation_impact_row
                  {where_sql}{cursor_sql}
                 ORDER BY ordinal {order}
                 LIMIT ?
                """,
                [*parameters, *cursor_parameters, page_size],
            ).fetchall()
            if not result and matching_count and (after is not None or before is not None):
                result = connection.execute(
                    f"""
                    SELECT ordinal, dataset, source_row, source_column,
                           target_field, raw_value, proposed_value, rules,
                           outcome, message
                      FROM transformation_impact_row
                      {where_sql}
                     ORDER BY ordinal
                     LIMIT ?
                    """,
                    [*parameters, page_size],
                ).fetchall()
                order = "ASC"
            if order == "DESC":
                result.reverse()
            ordinals = [int(item[0]) for item in result]
            if ordinals:
                preceding = connection.execute(
                    f"""
                    SELECT COUNT(*)
                      FROM transformation_impact_row
                      {where_sql} AND ordinal < ?
                    """,
                    [*parameters, ordinals[0]],
                ).fetchone()
                start_position = int(preceding[0]) + 1 if preceding else 1
            else:
                start_position = 0
            rows = tuple(
                TransformationImpactRow(
                    dataset=str(item[1]),
                    source_row=int(item[2]),
                    source_column=str(item[3]),
                    target_field=str(item[4]),
                    raw_value=str(item[5]),
                    proposed_value=str(item[6]),
                    rules=str(item[7]),
                    outcome=str(item[8]),
                    message=str(item[9]),
                )
                for item in result
            )
            end_position = (
                start_position + len(rows) - 1 if rows else 0
            )
        return TransformationImpactPage(
            rows=rows,
            matching_count=matching_count,
            start_position=start_position,
            end_position=end_position,
            previous_before=(
                ordinals[0] if ordinals and start_position > 1 else None
            ),
            next_after=(
                ordinals[-1]
                if ordinals and end_position < matching_count
                else None
            ),
        )
    def iter_transformation_impact_rows(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
    ) -> Iterator[TransformationImpactRow]:
        """Stream all matching snapshot rows in deterministic order."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            stored = connection.execute(
                """
                SELECT identity_hash
                  FROM transformation_impact_run
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if stored is None or str(stored[0]) != identity.content_hash:
                raise WorkspaceError("Prepare the current transformation impact first")
            where_sql, parameters = self._transformation_impact_where(filters)
            cursor = connection.execute(
                f"""
                SELECT dataset, source_row, source_column, target_field,
                       raw_value, proposed_value, rules, outcome, message
                  FROM transformation_impact_row
                  {where_sql}
                 ORDER BY ordinal
                """,
                parameters,
            )
            while batch := cursor.fetchmany(TRANSFORMATION_IMPACT_ROW_BATCH_SIZE):
                for item in batch:
                    yield TransformationImpactRow(
                        dataset=str(item[0]),
                        source_row=int(item[1]),
                        source_column=str(item[2]),
                        target_field=str(item[3]),
                        raw_value=str(item[4]),
                        proposed_value=str(item[5]),
                        rules=str(item[6]),
                        outcome=str(item[7]),
                        message=str(item[8]),
                    )
    @staticmethod
    def _transformation_impact_where(
        filters: TransformationImpactFilter,
    ) -> tuple[str, list[object]]:
        conditions = ["1 = 1"]
        parameters: list[object] = []
        for column, value in (
            ("dataset", filters.dataset),
            ("outcome", filters.outcome),
            ("target_field", filters.target_field),
        ):
            if value:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        if filters.query:
            conditions.append(
                "(" 
                "contains(lower(source_column), ?) OR "
                "contains(lower(target_field), ?) OR "
                "contains(lower(raw_value), ?) OR "
                "contains(lower(proposed_value), ?) OR "
                "contains(lower(rules), ?) OR "
                "contains(lower(message), ?)"
                ")"
            )
            parameters.extend([filters.query.lower()] * 6)
        return "WHERE " + " AND ".join(conditions), parameters
    @staticmethod
    def _transformation_impact_snapshot(
        identity: TransformationImpactIdentity,
        row: Sequence[object],
    ) -> TransformationImpactSnapshot:
        return TransformationImpactSnapshot(
            identity=identity,
            created_at=datetime.fromisoformat(str(row[8])),
            created_by=str(row[9]),
            affected_row_count=int(row[10]),
            report=TransformationImpactReport(
                mapping_content_hash=str(row[3]),
                evaluated_count=int(row[11]),
                changed_count=int(row[12]),
                fallback_count=int(row[13]),
                null_count=int(row[14]),
                invalid_count=int(row[15]),
                provided_count=int(row[16]),
                unchanged_count=int(row[17]),
                rows=(),
                detail_limit=0,
            ),
        )
