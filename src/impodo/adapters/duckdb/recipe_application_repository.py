"""Persist R3 Recipe application inputs and recoverable project-local state."""

from __future__ import annotations

import json

from ...access import Actor
from ...domain.recipe_applications import (
    RecipeApplicationDraft,
    RecipeApplicationEvidence,
    RecipeControlValues,
    RecipeParameterValues,
    TargetBinding,
)
from ...domain.serialization import canonical_json, content_hash
from ...projects import ProjectNotFoundError
from ...quality import QualityRule
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository


class RecipeApplicationRepository(DuckDbRepository):
    """Keep current binding inputs separate from reusable Recipe storage."""

    def get_target_binding(self, project_id: str) -> TargetBinding | None:
        value = self._read_singleton_json(
            project_id,
            """
            SELECT binding.binding_json
              FROM recipe_target_binding_current AS current
              JOIN recipe_target_binding AS binding
                ON binding.target_binding_id = current.target_binding_id
             WHERE current.singleton_id = 1
            """,
        )
        return TargetBinding.from_dict(json.loads(value)) if value else None

    def save_target_binding(
        self,
        project_id: str,
        binding: TargetBinding,
        *,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            revision = self._project_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO recipe_target_binding
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        binding.target_binding_id,
                        binding.content_hash,
                        binding.to_json(),
                        binding.probed_at.isoformat(),
                    ],
                )
                row = connection.execute(
                    """
                    SELECT content_hash FROM recipe_target_binding
                     WHERE target_binding_id = ?
                    """,
                    [binding.target_binding_id],
                ).fetchone()
                if row is None or str(row[0]) != binding.content_hash:
                    raise WorkspaceError("TargetBinding identity is already in use")
                connection.execute(
                    """
                    INSERT OR REPLACE INTO recipe_target_binding_current
                    VALUES (1, ?, ?)
                    """,
                    [binding.target_binding_id, binding.content_hash],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="RECIPE_TARGET_BINDING_ACCEPTED",
                    detail=(
                        f"{binding.environment.value} {binding.credential_role.value}; "
                        f"binding {binding.content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_parameter_values(
        self,
        project_id: str,
    ) -> RecipeParameterValues | None:
        value = self._read_singleton_json(
            project_id,
            "SELECT values_json FROM recipe_parameter_values WHERE singleton_id = 1",
        )
        return RecipeParameterValues.from_dict(json.loads(value)) if value else None

    def save_parameter_values(
        self,
        project_id: str,
        values: RecipeParameterValues,
        *,
        actor: Actor,
    ) -> None:
        self._save_values(
            project_id,
            table="recipe_parameter_values",
            content_hash=values.content_hash,
            payload=canonical_json(values.to_dict()),
            event_type="RECIPE_PARAMETER_VALUES_CONFIRMED",
            actor=actor,
        )

    def get_control_values(self, project_id: str) -> RecipeControlValues | None:
        value = self._read_singleton_json(
            project_id,
            "SELECT values_json FROM recipe_control_values WHERE singleton_id = 1",
        )
        return RecipeControlValues.from_dict(json.loads(value)) if value else None

    def save_control_values(
        self,
        project_id: str,
        values: RecipeControlValues,
        *,
        actor: Actor,
    ) -> None:
        self._save_values(
            project_id,
            table="recipe_control_values",
            content_hash=values.content_hash,
            payload=canonical_json(values.to_dict()),
            event_type="RECIPE_CONTROL_VALUES_CONFIRMED",
            actor=actor,
        )

    def get_draft(self, project_id: str) -> RecipeApplicationDraft | None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT application_id, recipe_id, recipe_revision,
                       data_version_id, target_binding_hash,
                       source_selection_hash, parameter_values_hash,
                       revision, state, overrides_json,
                       issue_fingerprints_json, updated_at
                  FROM recipe_application_draft
                 WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return None
        packed = json.loads(str(row[10]))
        payload = {
            "application_id": str(row[0]),
            "recipe_id": str(row[1]),
            "recipe_revision": int(row[2]),
            "data_version_id": str(row[3]),
            "workspace_project_id": project_id,
            "target_binding_hash": str(row[4]),
            "source_selection_hash": str(row[5]),
            "parameter_values_hash": str(row[6]),
            "revision": int(row[7]),
            "state": str(row[8]),
            "overrides": json.loads(str(row[9])),
            "issues": packed.get("items", []),
            "binding_hash": packed["_binding_hash"],
            "target_assessment_hash": packed["_target_assessment_hash"],
            "updated_at": str(row[11]),
            "updated_by": packed["_updated_by"],
        }
        return RecipeApplicationDraft.from_dict(payload)

    def save_draft(
        self,
        project_id: str,
        draft: RecipeApplicationDraft,
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> None:
        if draft.workspace_project_id != project_id:
            raise WorkspaceError("Application draft belongs to another workspace")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        packed_issues = {
            "_binding_hash": draft.binding_hash,
            "_target_assessment_hash": draft.target_assessment_hash,
            "_updated_by": {
                "issuer": draft.updated_by.issuer,
                "subject_id": draft.updated_by.subject_id,
                "display_name": draft.updated_by.display_name,
            },
            "items": [
                {
                    "code": item.code,
                    "level": item.level.value,
                    "message": item.message,
                    "recovery_action": item.recovery_action,
                    "logical_id": item.logical_id,
                    "fingerprint": item.fingerprint,
                }
                for item in draft.issues
            ],
        }
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                "SELECT revision FROM recipe_application_draft WHERE singleton_id = 1"
            ).fetchone()
            actual = int(row[0]) if row else None
            if actual != expected_revision:
                raise WorkspaceError(
                    "The Recipe application changed; reload before continuing"
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO recipe_application_draft VALUES (
                    1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    draft.application_id,
                    draft.recipe_id,
                    draft.recipe_revision,
                    draft.data_version_id,
                    draft.target_binding_hash,
                    draft.source_selection_hash,
                    draft.parameter_values_hash,
                    draft.revision,
                    draft.state.value,
                    canonical_json(dict(draft.overrides)),
                    canonical_json(packed_issues),
                    draft.updated_at.isoformat(),
                    draft.updated_by.display_name,
                ],
            )
            self._insert_workspace_audit(
                connection,
                revision=self._project_revision(connection),
                event_type="RECIPE_APPLICATION_REVIEWED",
                detail=(
                    f"application {draft.application_id}; state {draft.state.value}; "
                    f"issues {len(draft.issues)}"
                ),
                actor=actor,
            )

    def save_evidence_projection(
        self,
        project_id: str,
        *,
        application_id: str,
        content_hash: str,
        evidence_json: str,
        created_at: str,
    ) -> None:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            connection.execute(
                """
                INSERT INTO recipe_application_evidence VALUES (?, ?, ?, ?)
                """,
                [application_id, content_hash, evidence_json, created_at],
            )

    def get_evidence(
        self,
        project_id: str,
        application_id: str,
    ) -> RecipeApplicationEvidence | None:
        """Reload and verify one immutable application evidence projection."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            row = connection.execute(
                """
                SELECT content_hash, evidence_json
                  FROM recipe_application_evidence
                 WHERE application_id = ?
                """,
                [application_id],
            ).fetchone()
        if row is None:
            return None
        evidence = RecipeApplicationEvidence.from_dict(json.loads(str(row[1])))
        if (
            evidence.content_hash != str(row[0])
            or evidence.workspace_project_id != project_id
        ):
            raise WorkspaceError("Stored Recipe application evidence is invalid")
        return evidence

    def save_quality_seed(
        self,
        project_id: str,
        *,
        application_id: str,
        mapping_content_hash: str,
        rules: tuple[QualityRule, ...],
        actor: Actor,
    ) -> None:
        """Stage only reusable business rules for the fresh mapping hash."""

        rule_payload = [item.to_portable_dict() for item in rules]
        seed_hash = content_hash(
            {
                "application_id": application_id,
                "mapping_content_hash": mapping_content_hash,
                "rules": rule_payload,
            }
        )
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
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
                revision=self._project_revision(connection),
                event_type="RECIPE_QUALITY_SEED_STAGED",
                detail=(
                    f"application {application_id}; mapping {mapping_content_hash}; "
                    f"business rules {len(rules)}"
                ),
                actor=actor,
            )

    def get_quality_seed(
        self,
        project_id: str,
        mapping_content_hash: str,
    ) -> tuple[QualityRule, ...]:
        """Return a hash-verified seed only for its exact fresh mapping."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
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

    def _save_values(
        self,
        project_id: str,
        *,
        table: str,
        content_hash: str,
        payload: str,
        event_type: str,
        actor: Actor,
    ) -> None:
        if table not in {"recipe_parameter_values", "recipe_control_values"}:
            raise ValueError("Unsupported Recipe value table")
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_project_database_schema(connection)
            revision = self._project_revision(connection)
            connection.execute(
                f"INSERT OR REPLACE INTO {table} VALUES (1, ?, ?)",
                [content_hash, payload],
            )
            self._insert_workspace_audit(
                connection,
                revision=revision,
                event_type=event_type,
                detail=f"content {content_hash}",
                actor=actor,
            )
