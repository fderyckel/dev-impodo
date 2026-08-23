"""Persist editable Recipe compilation inputs in one workspace."""

from __future__ import annotations

from ...access import Actor
from ...domain.recipe_parameters import RecipeParameterDefinitions
from ...workspace_state import WorkspaceStateNotFoundError
from .repository import DuckDbRepository


class RecipeCompilationRepository(DuckDbRepository):
    """Store custom parameter declarations until Recipe publication."""

    def get_parameter_definitions(
        self,
        project_id: str,
    ) -> RecipeParameterDefinitions:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT definitions_json
              FROM recipe_parameter_definitions
             WHERE singleton_id = 1
            """,
        )
        return (
            RecipeParameterDefinitions.from_json(value)
            if value is not None
            else RecipeParameterDefinitions()
        )

    def save_parameter_definitions(
        self,
        project_id: str,
        definitions: RecipeParameterDefinitions,
        *,
        actor: Actor,
    ) -> None:
        database_path = self.workspace_directory(project_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            revision = self._workspace_revision(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO recipe_parameter_definitions
                VALUES (1, ?, ?)
                """,
                [definitions.content_hash, definitions.to_json()],
            )
            self._insert_workspace_audit(
                connection,
                revision=revision,
                event_type="RECIPE_PARAMETER_DEFINITIONS_SAVED",
                detail=(
                    f"definitions {len(definitions.definitions)}; "
                    f"content {definitions.content_hash}"
                ),
                actor=actor,
            )

