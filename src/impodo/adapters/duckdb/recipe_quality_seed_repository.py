"""Persist reusable quality rules for one fresh Recipe application mapping."""

from __future__ import annotations

import json

from impodo.application.workspace.access import WorkspaceAccessRepository
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.preparation.quality import QualityRule, RecipeQualitySeed
from impodo.domain.recipe.mapping_adaptation import recipe_mapping_shape
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError

from ...domain.serialization import canonical_json, content_hash
from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository
from .source_repository import WorkspaceSourceProjectionReader

_AUTHORING_SEED_PREFIX = "authoring:"


class RecipeQualitySeedRepository(DuckDbRepository):
    """Bind reusable checks to one exact fresh mapping hash."""

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        access_contexts: WorkspaceAccessRepository,
        *,
        projected_sources: WorkspaceSourceProjectionReader | None = None,
    ) -> None:
        super().__init__(database)
        self._access_contexts = access_contexts
        self._projected_sources = projected_sources

    def save_quality_seed(
        self,
        workspace_id: str,
        *,
        application_id: str,
        mapping_content_hash: str,
        mapping_definition: MappingDefinition,
        rules: tuple[QualityRule, ...],
        actor: Actor,
        coverage_scope_hash: str | None = None,
        reference_bundle_hash: str | None = None,
    ) -> None:
        self._assert_workspace_mutable(workspace_id)
        if mapping_definition.content_hash != mapping_content_hash:
            raise WorkspaceError("Recipe quality seed does not match its mapping")
        rule_payload: object = [item.to_portable_dict() for item in rules]
        if coverage_scope_hash is not None:
            rule_payload = {
                "rules": rule_payload,
                "coverage_scope_hash": coverage_scope_hash,
                "reference_bundle_hash": reference_bundle_hash,
            }
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
        return self.read_quality_seed(workspace_id, mapping_content_hash).rules

    def save_authoring_quality_seed(
        self, workspace_id: str, *, operation_id: str, definition: MappingDefinition,
        rules: tuple[QualityRule, ...], actor: Actor,
        coverage_scope_hash: str | None = None,
        reference_bundle_hash: str | None = None,
    ) -> None:
        """Stage editable business checks before completing draft creation."""
        self.save_quality_seed(
            workspace_id, application_id=f"{_AUTHORING_SEED_PREFIX}{operation_id}",
            mapping_content_hash=definition.content_hash, mapping_definition=definition,
            rules=rules, actor=actor,
            coverage_scope_hash=coverage_scope_hash,
            reference_bundle_hash=reference_bundle_hash,
        )

    def read_quality_seed(
        self, workspace_id: str, mapping_content_hash: str, *, editable_only: bool = False,
    ) -> RecipeQualitySeed:
        """Verify stored rules, requiring an exact mapping for a pinned run."""
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
            if row is not None:
                seed = self._decode_seed(row, mapping_content_hash, editable_only=editable_only)
                if seed.coverage_scope_hash is not None and not editable_only:
                    self._require_coverage_binding(connection, workspace_id, seed)
                return seed
        if not editable_only and self._access_contexts.resolve_workspace_access_context(
            workspace_id
        ).recipe_application_id is not None:
            raise WorkspaceError(
                "Recipe business checks are missing. Reapply the pinned Recipe "
                "before preparing this run."
            )
        return RecipeQualitySeed(())

    @staticmethod
    def _decode_seed(row, mapping_content_hash: str, *, editable_only: bool) -> RecipeQualitySeed:
        editable = str(row[0]).startswith(_AUTHORING_SEED_PREFIX)
        if editable_only and not editable:
            return RecipeQualitySeed(())
        if not editable and str(row[3]) != mapping_content_hash:
            raise WorkspaceError(
                "Confirm the current Recipe run decisions before preparing; "
                "its business checks belong to an earlier mapping."
            )
        payload = json.loads(str(row[1]))
        expected = content_hash(
            {
                "application_id": str(row[0]),
                "mapping_content_hash": str(row[3]),
                "rules": payload,
            }
        )
        if expected != str(row[2]):
            raise WorkspaceError("Stored Recipe quality seed is invalid")
        if isinstance(payload, list):
            return RecipeQualitySeed(
                tuple(QualityRule.from_dict(dict(item)) for item in payload), editable,
            )
        if not editable or not isinstance(payload, dict):
            raise WorkspaceError("Stored Recipe quality seed is invalid")
        return RecipeQualitySeed(
            tuple(QualityRule.from_dict(dict(item)) for item in payload["rules"]),
            editable, payload["coverage_scope_hash"], payload["reference_bundle_hash"],
        )
    def _require_coverage_binding(self, connection, workspace_id: str, seed: RecipeQualitySeed) -> None:
        """Keep a restored check bound to the approved selection and exact lists."""
        reference_hash = seed.reference_bundle_hash
        row = connection.execute(
            """
            SELECT scope.source_selection_hash, json_extract_string(source.selection_json, '$.content_hash')
              FROM coverage_scope_current AS active
              JOIN coverage_scope_revision AS scope
                ON scope.scope_id = active.scope_id AND scope.version = active.version
              LEFT JOIN source_selection AS source ON source.singleton_id = 1
              LEFT JOIN reference_bundle_current AS refs ON refs.singleton_id = 1
             WHERE active.singleton_id = 1 AND scope.content_hash = ?
               AND (? IS NULL OR refs.content_hash = ?)
            """,
            [seed.coverage_scope_hash, reference_hash, reference_hash],
        ).fetchone()
        if row is not None:
            expected, actual = row
            if actual is None and self._projected_sources is not None:
                selection = self._projected_sources.get_source_selection(workspace_id)
                actual = selection.content_hash if selection is not None else None
            if expected == actual:
                return
        raise WorkspaceError("The approved coverage scope or its reference lists changed.")

    def assert_mapping_adaptation(
        self, workspace_id: str, definition: MappingDefinition,
    ) -> None:
        """Reject authoring changes in an application before submission."""

        if self._access_contexts.resolve_workspace_access_context(
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
        fixed_controls = frozenset(
            expectation.control_id for dataset in baseline.datasets
            for expectation in dataset.control_expectations
        )
        if recipe_mapping_shape(baseline, fixed_control_ids=fixed_controls) != recipe_mapping_shape(
            definition, fixed_control_ids=fixed_controls,
        ):
            raise WorkspaceError(
                "This change alters the pinned Recipe or accepted delivery totals. "
                "Confirm target value matches here. Start a new run to change "
                "accepted totals, or publish a new Recipe version to change its rules."
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
