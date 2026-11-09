"""Persist editable Recipe compilation inputs in one workspace."""

from __future__ import annotations

from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError

from ...domain.recipe_parameters import RecipeParameterDefinitions
from .repository import DuckDbRepository


class RecipeCompilationRepository(DuckDbRepository):
    """Store custom parameter declarations until Recipe publication."""

    def get_parameter_definitions(
        self,
        workspace_id: str,
    ) -> RecipeParameterDefinitions:
        value = self._read_singleton_json(
            workspace_id,
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
        workspace_id: str,
        definitions: RecipeParameterDefinitions,
        *,
        actor: Actor,
        restore: bool = False,
    ) -> None:
        self._assert_workspace_mutable(workspace_id)
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            values = [definitions.content_hash, definitions.to_json()]
            if restore:
                inserted = connection.execute(
                    """
                    INSERT INTO recipe_parameter_definitions VALUES (1, ?, ?)
                    ON CONFLICT DO NOTHING RETURNING content_hash
                    """,
                    values,
                ).fetchone()
                if inserted is None:
                    current = connection.execute(
                        "SELECT content_hash FROM recipe_parameter_definitions WHERE singleton_id = 1"
                    ).fetchone()
                    if current is None or str(current[0]) != definitions.content_hash:
                        raise WorkspaceError("This draft already has different Recipe inputs.")
                    return
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO recipe_parameter_definitions VALUES (1, ?, ?)",
                    values,
                )
            self._insert_workspace_audit(
                connection,
                revision=self._workspace_revision(connection),
                event_type="RECIPE_PARAMETER_DEFINITIONS_SAVED",
                detail=(
                    f"definitions {len(definitions.definitions)}; "
                    f"content {definitions.content_hash}"
                ),
                actor=actor,
            )
