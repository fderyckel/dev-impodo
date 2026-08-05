"""DuckDB derived entity repository implementation."""

from __future__ import annotations



from ...access import Actor
from ...derived_entities import DerivedEntityPlan
from ...projects import ProjectNotFoundError
from ...workspace_contracts import SourceSelection
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository







class DerivedEntityRepository(DuckDbRepository):
    """Persistence operations for derived entity repository."""

    def get_derived_entity_plan(
        self,
        project_id: str,
    ) -> DerivedEntityPlan | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT revision.plan_json
              FROM derived_entity_plan_current AS current
              JOIN derived_entity_plan_revision AS revision
                ON revision.plan_id = current.plan_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return DerivedEntityPlan.from_json(value) if value else None
    def save_derived_entity_plan(
        self,
        project_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            selection_row = connection.execute(
                "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
            ).fetchone()
            if selection_row is None:
                raise WorkspaceError(
                    "Freeze source datasets before deriving entities"
                )
            selection = SourceSelection.from_json(str(selection_row[0]))
            if (
                plan.project_id != project_id
                or plan.source_selection_hash != selection.content_hash
            ):
                raise WorkspaceError(
                    "Derived-entity plan does not match the frozen source selection"
                )
            current = connection.execute(
                """
                SELECT plan_id, version
                  FROM derived_entity_plan_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            actual_parent = int(current[1]) if current else None
            expected_plan_id = str(current[0]) if current else plan.plan_id
            if (
                actual_parent != expected_parent_version
                or plan.version != (actual_parent or 0) + 1
                or plan.plan_id != expected_plan_id
            ):
                raise WorkspaceError(
                    "The derived-entity plan was modified by another request"
                )
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO derived_entity_plan_revision
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        plan.plan_id,
                        plan.version,
                        plan.source_selection_hash,
                        plan.content_hash,
                        plan.updated_at.isoformat(),
                        plan.updated_by,
                        plan.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO derived_entity_plan_current
                    VALUES (1, ?, ?)
                    """,
                    [plan.plan_id, plan.version],
                )
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="DERIVED_ENTITY_PLAN_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="DERIVED_ENTITY_PLAN_SAVED",
                    detail=f"version {plan.version}: {len(plan.rules)} rule(s)",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
