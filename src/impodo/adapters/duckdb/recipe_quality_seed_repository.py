"""Persist reusable quality rules for one fresh Recipe application mapping."""

from __future__ import annotations

import json

from impodo.domain.shared.access import Actor
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
        rules: tuple[QualityRule, ...],
        actor: Actor,
    ) -> None:
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
                VALUES (1, ?, ?, ?, ?, current_timestamp)
                """,
                [
                    application_id,
                    mapping_content_hash,
                    canonical_json(rule_payload),
                    seed_hash,
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
                SELECT application_id, rules_json, content_hash
                  FROM recipe_quality_seed
                 WHERE singleton_id = 1 AND mapping_content_hash = ?
                """,
                [mapping_content_hash],
            ).fetchone()
        if row is None:
            return ()
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

