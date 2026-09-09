"""Persist reusable quality rules for one fresh Recipe application mapping."""

from __future__ import annotations

import json

from impodo.domain.shared.access import Actor
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.recipe.mapping_adaptation import recipe_mapping_shape
from ...domain.serialization import canonical_json, content_hash
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError
from impodo.domain.preparation.quality import QualityRule
from impodo.domain.workspace.errors import WorkspaceError
from .repository import DuckDbRepository


class RecipeQualitySeedRepository(DuckDbRepository):
    """Bind reusable checks to one exact fresh mapping hash."""

    def save_quality_seed(
        self,
        workspace_id: str,
        *,
        application_id: str,
        mapping_content_hash: str,
        mapping_definition: MappingDefinition,
        rules: tuple[QualityRule, ...],
        actor: Actor,
    ) -> None:
        self._assert_workspace_mutable(workspace_id)
        if mapping_definition.content_hash != mapping_content_hash:
            raise WorkspaceError("Recipe quality seed does not match its mapping")
        rule_payload = [item.to_portable_dict() for item in rules]
        seed_hash = content_hash(
            {
                "application_id": application_id,
                "mapping_content_hash": mapping_content_hash,
                "rules": rule_payload,
            }
        )
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.execute(
                """
                INSERT OR REPLACE INTO recipe_quality_seed
                VALUES (1, ?, ?, ?, ?, current_timestamp, ?)
                """,
                [
                    application_id,
                    mapping_content_hash,
                    canonical_json(rule_payload),
                    seed_hash,
                    mapping_definition.to_json(),
                ],
            )
            self._insert_workspace_audit(
                connection,
                revision=self._workspace_revision(connection),
                event_type="RECIPE_QUALITY_SEED_STAGED",
                detail=(
                    f"application {application_id}; mapping {mapping_content_hash}; "
                    f"business rules {len(rules)}"
                ),
                actor=actor,
            )

    def get_quality_seed(
        self,
        workspace_id: str,
        mapping_content_hash: str,
    ) -> tuple[QualityRule, ...]:
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("MigrationWorkspace not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT application_id, rules_json, content_hash, mapping_content_hash
                  FROM recipe_quality_seed
                 WHERE singleton_id = 1
                """,
            ).fetchone()
        if row is None:
            if self._database.resolve_workspace_access_context(
                workspace_id
            ).recipe_application_id is not None:
                raise WorkspaceError(
                    "Recipe business checks are missing. Reapply the pinned Recipe "
                    "before preparing this run."
                )
            return ()
        if str(row[3]) != mapping_content_hash:
            raise WorkspaceError(
                "Confirm the current Recipe run decisions before preparing; "
                "its business checks belong to an earlier mapping."
            )
        payload = json.loads(str(row[1]))
        expected = content_hash(
            {
                "application_id": str(row[0]),
                "mapping_content_hash": mapping_content_hash,
                "rules": payload,
            }
        )
        if expected != str(row[2]):
            raise WorkspaceError("Stored Recipe quality seed is invalid")
        return tuple(QualityRule.from_dict(dict(item)) for item in payload)

    def assert_mapping_adaptation(
        self, workspace_id: str, definition: MappingDefinition,
    ) -> None:
        """Reject authoring changes in an application before submission."""

        if self._database.resolve_workspace_access_context(
            workspace_id
        ).recipe_application_id is None:
            return
        with self._connect(
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        ) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                "SELECT mapping_content_hash, mapping_definition_json "
                "FROM recipe_quality_seed WHERE singleton_id = 1"
            ).fetchone()
        if row is not None and str(row[0]) == definition.content_hash:
            return
        if row is None or row[1] is None:
            raise WorkspaceError(
                "This application has no saved Recipe mapping baseline. "
                "Reapply the pinned Recipe before changing run decisions."
            )
        baseline = MappingDefinition.from_dict(json.loads(str(row[1])))
        if recipe_mapping_shape(baseline) != recipe_mapping_shape(definition):
            raise WorkspaceError(
                "This change alters the pinned Recipe. Run decisions may change "
                "target value matches and current control expectations only. "
                "Publish a new Recipe version for other changes."
            )

    def rebind_quality_seed(
        self, workspace_id: str, *, definition: MappingDefinition, actor: Actor,
    ) -> None:
        """Carry the same verified rules through a permitted run decision."""

        self._assert_workspace_mutable(workspace_id)
        self.assert_mapping_adaptation(workspace_id, definition)
        with self._connect(
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        ) as connection:
            row = connection.execute(
                "SELECT application_id, mapping_content_hash, rules_json, content_hash "
                "FROM recipe_quality_seed WHERE singleton_id = 1"
            ).fetchone()
            if row is None:
                raise WorkspaceError("Recipe business checks are missing")
            payload = {
                "application_id": str(row[0]),
                "mapping_content_hash": str(row[1]),
                "rules": json.loads(str(row[2])),
            }
            if content_hash(payload) != str(row[3]):
                raise WorkspaceError("Stored Recipe quality seed is invalid")
            if str(row[1]) == definition.content_hash:
                return
            payload["mapping_content_hash"] = definition.content_hash
            connection.execute(
                "UPDATE recipe_quality_seed SET mapping_content_hash = ?, "
                "content_hash = ? WHERE singleton_id = 1",
                [definition.content_hash, content_hash(payload)],
            )
            self._insert_workspace_audit(
                connection, revision=self._workspace_revision(connection),
                event_type="RECIPE_QUALITY_SEED_REBOUND",
                detail=f"mapping {definition.content_hash}", actor=actor,
            )
