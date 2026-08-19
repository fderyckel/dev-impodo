"""Persist Recipe aggregate projections and restart-safe lifecycle intents."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Mapping
from uuid import uuid4

from ...access import ActorIdentity
from ...domain.serialization import canonical_json
from ...domain.recipe_qualifications import (
    CutoverCandidateRecord,
    RecipeQualificationRecord,
)
from ...projects import ProjectError, ProjectNotFoundError
from ...recipes import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
    Recipe,
    RecipeConflictError,
    RecipeIdentifierConfusionError,
    RecipeIntent,
    RecipeIntentKind,
    RecipeIntentState,
    RecipeRevision,
    RecipeNotFoundError,
    RecipeSummary,
    WorkspaceResolution,
    require_hash,
    require_uuid,
)
from .database import DuckDbDatabase
from .repository import DuckDbRepository


class RecipeRepository(DuckDbRepository):
    """Own Recipe registry rows without scanning contained project databases."""

    def __init__(self, database: DuckDbDatabase) -> None:
        super().__init__(database)

    def list(self) -> tuple[RecipeSummary, ...]:
        """Return bounded Recipe cards using the registry database only."""

        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT r.recipe_id, r.display_name, r.current_recipe_revision,
                       r.current_data_version_id,
                       current_data.workspace_project_id,
                       project.revision,
                       (SELECT count(*) FROM data_version d
                         WHERE d.recipe_id = r.recipe_id) AS data_version_count,
                       (SELECT q.status FROM recipe_qualification q
                         WHERE q.recipe_id = r.recipe_id
                           AND q.recipe_revision = r.current_recipe_revision
                         ORDER BY q.qualified_at DESC, q.qualification_id DESC
                         LIMIT 1) AS qualification_status,
                       (SELECT c.recipe_revision FROM cutover_candidate c
                         WHERE c.cutover_candidate_id = r.cutover_candidate_id
                       ) AS cutover_revision,
                       (
                           r.current_recipe_revision IS NULL
                           AND (SELECT count(*) FROM data_version d
                                 WHERE d.recipe_id = r.recipe_id) = 1
                           AND NOT EXISTS (
                               SELECT 1 FROM recipe_qualification q
                                WHERE q.recipe_id = r.recipe_id
                           )
                           AND NOT EXISTS (
                               SELECT 1 FROM cutover_candidate c
                                WHERE c.recipe_id = r.recipe_id
                           )
                       ) AS deletable,
                       r.optimistic_revision, r.updated_at
                  FROM recipe r
             LEFT JOIN data_version current_data
                    ON current_data.data_version_id = r.current_data_version_id
             LEFT JOIN project_registry project
                    ON project.project_id = current_data.workspace_project_id
                 ORDER BY r.updated_at DESC, r.recipe_id
                """
            ).fetchall()
        return tuple(
            RecipeSummary(
                recipe_id=str(row[0]),
                display_name=str(row[1]),
                current_recipe_revision=(int(row[2]) if row[2] is not None else None),
                current_data_version_id=(str(row[3]) if row[3] else None),
                current_workspace_project_id=(str(row[4]) if row[4] else None),
                current_workspace_revision=(
                    int(row[5]) if row[5] is not None else None
                ),
                data_version_count=int(row[6]),
                deletable=bool(row[9]),
                qualification_status=(str(row[7]) if row[7] else None),
                cutover_recipe_revision=(int(row[8]) if row[8] is not None else None),
                optimistic_revision=int(row[10]),
                updated_at=datetime.fromisoformat(str(row[11])),
            )
            for row in rows
        )

    def get(self, recipe_id: str) -> Recipe:
        """Load one exact Recipe aggregate projection."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT * FROM recipe WHERE recipe_id = ?",
                [recipe_id],
            ).fetchone()
            columns = [item[0] for item in connection.description]
        if row is None:
            raise RecipeNotFoundError("Recipe not found")
        return self._recipe(dict(zip(columns, row, strict=True)))

    def delete_draft(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        expected_workspace_revision: int,
    ) -> str:
        """Delete one unpublished Recipe and its sole contained workspace."""

        project_id = self.validate_draft_deletion(
            recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            expected_workspace_revision=expected_workspace_revision,
        )
        recipe_id = require_uuid(recipe_id, "recipe_id")
        project_dir = self.project_directory(project_id)
        database_path = project_dir / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Recipe workspace not found")
        staged: Path = self.root / f".{project_id}.deleting-{uuid4()}"
        project_dir.rename(staged)
        registry_deleted = False
        try:
            with self._connect(self.registry_path) as connection:
                connection.begin()
                current = connection.execute(
                    """
                    SELECT r.optimistic_revision, p.revision
                      FROM recipe r
                      JOIN data_version d
                        ON d.data_version_id = r.current_data_version_id
                      JOIN project_registry p
                        ON p.project_id = d.workspace_project_id
                     WHERE r.recipe_id = ? AND d.workspace_project_id = ?
                    """,
                    [recipe_id, project_id],
                ).fetchone()
                if current != (
                    expected_recipe_revision,
                    expected_workspace_revision,
                ):
                    raise RecipeConflictError(
                        "Recipe changed during deletion; reload before retrying"
                    )
                connection.execute(
                    "DELETE FROM data_version WHERE recipe_id = ?",
                    [recipe_id],
                )
                connection.execute(
                    "DELETE FROM recipe_intent WHERE recipe_id = ?",
                    [recipe_id],
                )
                connection.execute(
                    "DELETE FROM recipe WHERE recipe_id = ?",
                    [recipe_id],
                )
                connection.execute(
                    "DELETE FROM project_registry WHERE project_id = ?",
                    [project_id],
                )
                connection.execute(
                    "DELETE FROM project_registry_sync_pending WHERE project_id = ?",
                    [project_id],
                )
                connection.commit()
                registry_deleted = True
            shutil.rmtree(staged)
        except Exception:
            if not registry_deleted and staged.exists() and not project_dir.exists():
                staged.rename(project_dir)
            raise
        return project_id

    def validate_draft_deletion(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        expected_workspace_revision: int,
    ) -> str:
        """Validate an exact draft deletion before any external cleanup."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT r.optimistic_revision, d.workspace_project_id,
                       p.revision,
                       (SELECT count(*) FROM data_version x
                         WHERE x.recipe_id = r.recipe_id),
                       (SELECT count(*) FROM recipe_revision x
                         WHERE x.recipe_id = r.recipe_id),
                       (SELECT count(*) FROM recipe_application x
                         WHERE x.recipe_id = r.recipe_id),
                       (SELECT count(*) FROM recipe_qualification x
                         WHERE x.recipe_id = r.recipe_id),
                       (SELECT count(*) FROM cutover_candidate x
                         WHERE x.recipe_id = r.recipe_id)
                  FROM recipe r
                  JOIN data_version d
                    ON d.data_version_id = r.current_data_version_id
                  JOIN project_registry p
                    ON p.project_id = d.workspace_project_id
                 WHERE r.recipe_id = ? AND r.current_recipe_revision IS NULL
                   AND d.purpose = 'AUTHORING' AND d.state = 'ACTIVE'
                """,
                [recipe_id],
            ).fetchone()
        if row is None:
            raise RecipeConflictError("Only an unpublished Recipe can be deleted")
        if int(row[0]) != expected_recipe_revision:
            raise RecipeConflictError("Recipe changed; reload before deleting")
        if int(row[2]) != expected_workspace_revision:
            raise RecipeConflictError("Recipe workspace changed; reload before deleting")
        if tuple(int(value) for value in row[3:]) != (1, 0, 0, 0, 0):
            raise RecipeConflictError("Recipe has reusable evidence and cannot be deleted")
        return str(row[1])

    def data_versions(self, recipe_id: str) -> tuple[DataVersion, ...]:
        """Return bounded DataVersion lineage for one Recipe."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM data_version
                 WHERE recipe_id = ?
                 ORDER BY version_number
                """,
                [recipe_id],
            ).fetchall()
            columns = [item[0] for item in connection.description]
        if not rows:
            self.get(recipe_id)
        return tuple(
            self._data_version(dict(zip(columns, row, strict=True))) for row in rows
        )

    def update_unpublished_setup(
        self,
        recipe_id: str,
        *,
        workspace_project_id: str,
        display_name: str,
        business_purpose: str,
        data_classification: str,
        retention_days: int,
    ) -> Recipe:
        """Synchronize Recipe-owned setup while its first revision is unpublished."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        workspace_project_id = require_uuid(
            workspace_project_id,
            "workspace_project_id",
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(self.registry_path) as connection:
            updated = connection.execute(
                """
                UPDATE recipe
                   SET display_name = ?, business_purpose = ?,
                       data_classification = ?, retention_days = ?,
                       optimistic_revision = optimistic_revision + 1,
                       updated_at = ?
                 WHERE recipe_id = ? AND current_recipe_revision IS NULL
                   AND current_data_version_id = (
                       SELECT data_version_id FROM data_version
                        WHERE recipe_id = ? AND workspace_project_id = ?
                          AND purpose = 'AUTHORING' AND state = 'ACTIVE'
                   )
                 RETURNING recipe_id
                """,
                [
                    display_name,
                    business_purpose,
                    data_classification,
                    retention_days,
                    now,
                    recipe_id,
                    recipe_id,
                    workspace_project_id,
                ],
            ).fetchone()
        if updated is None:
            raise RecipeConflictError("Only unpublished Recipe setup can change")
        return self.get(recipe_id)

    def update_data_version_parameter_values_hash(
        self,
        recipe_id: str,
        data_version_id: str,
        *,
        expected_hash: str,
        parameter_values_hash: str,
    ) -> DataVersion:
        """Move one active DataVersion to newly confirmed parameter evidence."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        require_hash(expected_hash, "expected_hash")
        require_hash(parameter_values_hash, "parameter_values_hash")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                UPDATE data_version
                   SET parameter_values_hash = ?
                 WHERE recipe_id = ? AND data_version_id = ?
                   AND state = 'ACTIVE' AND parameter_values_hash = ?
                   AND EXISTS (
                       SELECT 1 FROM recipe r
                        WHERE r.recipe_id = data_version.recipe_id
                          AND r.current_data_version_id = data_version.data_version_id
                   )
                 RETURNING *
                """,
                [
                    parameter_values_hash,
                    recipe_id,
                    data_version_id,
                    expected_hash,
                ],
            ).fetchone()
            columns = [item[0] for item in connection.description]
        if row is None:
            raise RecipeConflictError(
                "Recipe parameter values changed; reload before saving them"
            )
        return self._data_version(dict(zip(columns, row, strict=True)))

    def revision_record(
        self,
        recipe_id: str,
        version: int,
    ) -> dict[str, object]:
        """Return the bounded protected-storage manifest for one revision."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        if version < 1:
            raise RecipeNotFoundError("Recipe revision not found")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT semantic_hash, payload_hash, storage_key, artifact_hash,
                       size_bytes, contract_versions_json, provenance_json,
                       published_at
                  FROM recipe_revision
                 WHERE recipe_id = ? AND version = ?
                """,
                [recipe_id, version],
            ).fetchone()
        if row is None:
            raise RecipeNotFoundError("Recipe revision not found")
        return {
            "artifact_hash": str(row[3]),
            "contract_versions": json.loads(str(row[5])),
            "payload_hash": str(row[1]),
            "provenance": json.loads(str(row[6])),
            "published_at": str(row[7]),
            "recipe_id": recipe_id,
            "semantic_hash": str(row[0]),
            "size_bytes": int(row[4]),
            "storage_key": str(row[2]),
            "version": version,
        }

    def revisions(self, recipe_id: str) -> tuple[RecipeRevision, ...]:
        """Return immutable revision lineage without reading protected payloads."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT version, parent_version, semantic_hash, payload_hash,
                       storage_key, artifact_hash, size_bytes,
                       contract_versions_json, provenance_json,
                       actor_issuer, actor_subject, actor_display_name,
                       published_at
                  FROM recipe_revision
                 WHERE recipe_id = ?
                 ORDER BY version
                """,
                [recipe_id],
            ).fetchall()
        return tuple(
            RecipeRevision(
                recipe_id=recipe_id,
                version=int(row[0]),
                parent_version=(int(row[1]) if row[1] is not None else None),
                semantic_hash=str(row[2]),
                payload_hash=str(row[3]),
                storage_key=str(row[4]),
                artifact_hash=str(row[5]),
                size_bytes=int(row[6]),
                contract_versions=json.loads(str(row[7])),
                provenance=json.loads(str(row[8])),
                published_by=ActorIdentity(str(row[9]), str(row[10]), str(row[11])),
                published_at=datetime.fromisoformat(str(row[12])),
            )
            for row in rows
        )

    def qualifications(
        self,
        recipe_id: str,
    ) -> tuple[RecipeQualificationRecord, ...]:
        """Return bounded immutable qualification lineage."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT qualification_id, recipe_revision, application_id,
                       test_target_binding_hash, status, findings_json,
                       actor_issuer, actor_subject, actor_display_name,
                       qualified_at, evidence_storage_key, evidence_hash
                  FROM recipe_qualification
                 WHERE recipe_id = ?
                 ORDER BY qualified_at, qualification_id
                """,
                [recipe_id],
            ).fetchall()
        return tuple(
            RecipeQualificationRecord(
                qualification_id=str(row[0]),
                recipe_id=recipe_id,
                recipe_revision=int(row[1]),
                application_id=str(row[2]),
                test_target_binding_hash=str(row[3]),
                status=str(row[4]),
                findings=tuple(dict(item) for item in json.loads(str(row[5]))),
                qualified_by=ActorIdentity(str(row[6]), str(row[7]), str(row[8])),
                qualified_at=datetime.fromisoformat(str(row[9])),
                evidence_storage_key=str(row[10]),
                evidence_hash=str(row[11]),
            )
            for row in rows
        )

    def current_qualification(
        self,
        recipe_id: str,
    ) -> RecipeQualificationRecord | None:
        """Return only a qualification for the Recipe's current revision."""

        recipe = self.get(recipe_id)
        if recipe.current_recipe_revision is None:
            return None
        return next(
            (
                item
                for item in reversed(self.qualifications(recipe_id))
                if item.recipe_revision == recipe.current_recipe_revision
            ),
            None,
        )

    def cutover_candidate(
        self,
        recipe_id: str,
    ) -> CutoverCandidateRecord | None:
        """Return the exact selected rollout candidate, if one exists."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT cutover_candidate_id, recipe_revision, qualification_id,
                       actor_issuer, actor_subject, actor_display_name,
                       selected_at, content_hash
                  FROM cutover_candidate
                 WHERE cutover_candidate_id = (
                       SELECT cutover_candidate_id FROM recipe WHERE recipe_id = ?
                 )
                """,
                [recipe_id],
            ).fetchone()
        if row is None:
            return None
        return CutoverCandidateRecord(
            cutover_candidate_id=str(row[0]),
            recipe_id=recipe_id,
            recipe_revision=int(row[1]),
            qualification_id=str(row[2]),
            selected_by=ActorIdentity(str(row[3]), str(row[4]), str(row[5])),
            selected_at=datetime.fromisoformat(str(row[6])),
            content_hash=str(row[7]),
        )

    def revision_version_by_semantic_hash(
        self,
        recipe_id: str,
        semantic_hash: str,
    ) -> int | None:
        """Return an existing immutable version with the same reusable meaning."""

        recipe_id = require_uuid(recipe_id, "recipe_id")
        require_hash(semantic_hash, "semantic_hash")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT version FROM recipe_revision
                 WHERE recipe_id = ? AND semantic_hash = ?
                """,
                [recipe_id, semantic_hash],
            ).fetchone()
        return int(row[0]) if row is not None else None

    def resolve_workspace(self, workspace_project_id: str) -> WorkspaceResolution:
        """Resolve only a workspace project ID and reject namespace confusion."""

        candidate = require_uuid(workspace_project_id, "workspace_project_id")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT d.recipe_id, d.data_version_id, d.version_number,
                       d.workspace_project_id, d.purpose, d.state
                  FROM data_version d
                  JOIN recipe r ON r.recipe_id = d.recipe_id
                 WHERE d.workspace_project_id = ?
                """,
                [candidate],
            ).fetchone()
            if row is None:
                confused = connection.execute(
                    """
                    SELECT
                        EXISTS (SELECT 1 FROM recipe WHERE recipe_id = ?),
                        EXISTS (SELECT 1 FROM data_version WHERE data_version_id = ?)
                    """,
                    [candidate, candidate],
                ).fetchone()
                if confused and (bool(confused[0]) or bool(confused[1])):
                    raise RecipeIdentifierConfusionError(
                        "Recipe and DataVersion IDs cannot be used as project IDs"
                    )
                raise ProjectNotFoundError("Project not found")
        return WorkspaceResolution(
            recipe_id=str(row[0]),
            data_version_id=str(row[1]),
            data_version_number=int(row[2]),
            workspace_project_id=str(row[3]),
            data_version_purpose=DataVersionPurpose(str(row[4])),
            data_version_state=DataVersionState(str(row[5])),
        )

    def reserve_intent(
        self,
        *,
        operation_id: str,
        recipe_id: str,
        kind: RecipeIntentKind,
        expected_recipe_revision: int,
        detail: Mapping[str, object],
    ) -> RecipeIntent:
        """Persist an idempotent operation before crossing a store boundary."""

        operation_id = require_uuid(operation_id, "operation_id")
        recipe_id = require_uuid(recipe_id, "recipe_id")
        kind = RecipeIntentKind(kind)
        if expected_recipe_revision < 1:
            raise RecipeConflictError("Expected Recipe revision is invalid")
        detail_json = canonical_json(detail)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(self.registry_path) as connection:
            existing = connection.execute(
                "SELECT * FROM recipe_intent WHERE operation_id = ?",
                [operation_id],
            ).fetchone()
            if existing is not None:
                columns = [item[0] for item in connection.description]
                current = self._intent(dict(zip(columns, existing, strict=True)))
                if (
                    current.recipe_id != recipe_id
                    or current.kind is not kind
                    or current.expected_recipe_revision != expected_recipe_revision
                    or canonical_json(current.detail) != detail_json
                ):
                    raise RecipeConflictError("Operation ID is already in use")
                return current
        recipe = self.get(recipe_id)
        if recipe.optimistic_revision != expected_recipe_revision:
            raise RecipeConflictError("Recipe changed; reload before continuing")
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO recipe_intent
                VALUES (?, ?, ?, 'RESERVED', ?, ?, '', ?, ?)
                """,
                [
                    operation_id,
                    recipe_id,
                    kind.value,
                    expected_recipe_revision,
                    detail_json,
                    now,
                    now,
                ],
            )
        return self.get_intent(operation_id)

    def get_intent(self, operation_id: str) -> RecipeIntent:
        operation_id = require_uuid(operation_id, "operation_id")
        with self._connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT * FROM recipe_intent WHERE operation_id = ?",
                [operation_id],
            ).fetchone()
            columns = [item[0] for item in connection.description]
        if row is None:
            raise RecipeNotFoundError("Recipe operation not found")
        return self._intent(dict(zip(columns, row, strict=True)))

    def incomplete_intents(self) -> tuple[RecipeIntent, ...]:
        """Return only operations that require deterministic recovery."""

        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM recipe_intent
                 WHERE state NOT IN ('COMPLETE', 'ABANDONED')
                 ORDER BY created_at, operation_id
                """
            ).fetchall()
            columns = [item[0] for item in connection.description]
        return tuple(self._intent(dict(zip(columns, row, strict=True))) for row in rows)

    def transition_intent(
        self,
        operation_id: str,
        *,
        expected_state: RecipeIntentState,
        new_state: RecipeIntentState,
        detail: Mapping[str, object] | None = None,
        last_error: str = "",
    ) -> RecipeIntent:
        """Optimistically advance one intent while retaining bounded detail."""

        operation_id = require_uuid(operation_id, "operation_id")
        expected_state = RecipeIntentState(expected_state)
        new_state = RecipeIntentState(new_state)
        current = self.get_intent(operation_id)
        if current.state is new_state:
            return current
        if current.state is not expected_state:
            raise RecipeConflictError("Recipe operation state changed")
        next_detail = detail if detail is not None else current.detail
        encoded_error = last_error.strip()[:1000]
        with self._connect(self.registry_path) as connection:
            updated = connection.execute(
                """
                UPDATE recipe_intent
                   SET state = ?, detail_json = ?, last_error = ?, updated_at = ?
                 WHERE operation_id = ? AND state = ?
                 RETURNING operation_id
                """,
                [
                    new_state.value,
                    canonical_json(next_detail),
                    encoded_error,
                    datetime.now(timezone.utc).isoformat(),
                    operation_id,
                    expected_state.value,
                ],
            )
            if updated.fetchone() is None:
                raise RecipeConflictError("Recipe operation state changed")
        return self.get_intent(operation_id)

    def commit_publication(self, operation_id: str) -> RecipeIntent:
        """Atomically append one stored RecipeRevision and advance its pointer."""

        intent = self.get_intent(operation_id)
        if intent.kind is not RecipeIntentKind.RECIPE_PUBLICATION:
            raise RecipeConflictError("Recipe operation kind is invalid")
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            return intent
        if intent.state is not RecipeIntentState.PAYLOAD_STORED:
            raise RecipeConflictError("Recipe payload is not stored")
        detail = intent.detail
        version = int(detail["version"])
        semantic_hash = require_hash(str(detail["semantic_hash"]), "semantic_hash")
        payload_hash = require_hash(str(detail["payload_hash"]), "payload_hash")
        artifact_hash = require_hash(str(detail["artifact_hash"]), "artifact_hash")
        actor = self._actor_detail(detail)
        with self._connect(self.registry_path) as connection:
            connection.begin()
            try:
                recipe_row = connection.execute(
                    """
                    SELECT current_recipe_revision, optimistic_revision
                      FROM recipe WHERE recipe_id = ?
                    """,
                    [intent.recipe_id],
                ).fetchone()
                if recipe_row is None:
                    raise RecipeNotFoundError("Recipe not found")
                if int(recipe_row[1]) != intent.expected_recipe_revision:
                    raise RecipeConflictError("Recipe changed before publication")
                current_version = (
                    int(recipe_row[0]) if recipe_row[0] is not None else None
                )
                expected_version = 1 if current_version is None else current_version + 1
                if version != expected_version:
                    raise RecipeConflictError("Recipe version is not the next revision")
                connection.execute(
                    """
                    INSERT INTO recipe_revision VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        intent.recipe_id,
                        version,
                        current_version,
                        semantic_hash,
                        payload_hash,
                        str(detail["storage_key"]),
                        artifact_hash,
                        int(detail["size_bytes"]),
                        canonical_json(detail["contract_versions"]),
                        canonical_json(detail["provenance"]),
                        actor.issuer,
                        actor.subject_id,
                        actor.display_name,
                        str(detail["published_at"]),
                    ],
                )
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE recipe
                       SET current_recipe_revision = ?, optimistic_revision = ?,
                           updated_at = ?
                     WHERE recipe_id = ?
                    """,
                    [
                        version,
                        intent.expected_recipe_revision + 1,
                        now,
                        intent.recipe_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE recipe_intent
                       SET state = 'REGISTRY_COMMITTED', updated_at = ?
                     WHERE operation_id = ?
                    """,
                    [now, operation_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_intent(operation_id)

    def commit_data_version(self, operation_id: str) -> RecipeIntent:
        """Link an already provisioned workspace and activate its DataVersion."""

        intent = self.get_intent(operation_id)
        if intent.kind is not RecipeIntentKind.DATA_VERSION_CREATION:
            raise RecipeConflictError("Recipe operation kind is invalid")
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            return intent
        if intent.state is not RecipeIntentState.RESERVED:
            raise RecipeConflictError("DataVersion operation is not reserved")
        detail = intent.detail
        data_version_id = require_uuid(
            str(detail["data_version_id"]), "data_version_id"
        )
        workspace_project_id = require_uuid(
            str(detail["workspace_project_id"]), "workspace_project_id"
        )
        if data_version_id in {intent.recipe_id, workspace_project_id}:
            raise RecipeIdentifierConfusionError("Recipe identities must be distinct")
        purpose = DataVersionPurpose(str(detail["purpose"]))
        with self._connect(self.registry_path) as connection:
            connection.begin()
            try:
                recipe_row = connection.execute(
                    """
                    SELECT current_recipe_revision, current_data_version_id,
                           cutover_candidate_id, optimistic_revision
                      FROM recipe WHERE recipe_id = ?
                    """,
                    [intent.recipe_id],
                ).fetchone()
                if recipe_row is None:
                    raise RecipeNotFoundError("Active Recipe not found")
                if int(recipe_row[3]) != intent.expected_recipe_revision:
                    raise RecipeConflictError(
                        "Recipe changed before DataVersion creation"
                    )
                pinned_recipe_revision = (
                    int(detail["pinned_recipe_revision"])
                    if detail.get("pinned_recipe_revision") is not None
                    else None
                )
                if purpose is DataVersionPurpose.TEST:
                    if (
                        recipe_row[0] is None
                        or pinned_recipe_revision != int(recipe_row[0])
                    ):
                        raise RecipeConflictError(
                            "Test data version no longer pins the current Recipe revision"
                        )
                elif purpose is DataVersionPurpose.PRODUCTION:
                    candidate_id = require_uuid(
                        str(detail.get("cutover_candidate_id")),
                        "cutover_candidate_id",
                    )
                    if not recipe_row[2] or candidate_id != str(recipe_row[2]):
                        raise RecipeConflictError(
                            "The selected rollout candidate changed before Production started"
                        )
                    candidate = connection.execute(
                        """
                        SELECT recipe_revision
                          FROM cutover_candidate
                         WHERE cutover_candidate_id = ? AND recipe_id = ?
                        """,
                        [candidate_id, intent.recipe_id],
                    ).fetchone()
                    if (
                        candidate is None
                        or pinned_recipe_revision != int(candidate[0])
                    ):
                        raise RecipeConflictError(
                            "Production no longer pins the selected qualified Recipe revision"
                        )
                elif pinned_recipe_revision is not None:
                    raise RecipeConflictError(
                        "An authoring data version cannot pin a published Recipe revision"
                    )
                project_exists = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM project_registry WHERE project_id = ?
                    )
                    """,
                    [workspace_project_id],
                ).fetchone()
                if not project_exists or not bool(project_exists[0]):
                    raise ProjectNotFoundError("Workspace project not found")
                collision = connection.execute(
                    """
                    SELECT
                        EXISTS (SELECT 1 FROM recipe WHERE recipe_id = ?),
                        EXISTS (SELECT 1 FROM data_version WHERE data_version_id = ?),
                        EXISTS (SELECT 1 FROM project_registry WHERE project_id = ?)
                    """,
                    [data_version_id, data_version_id, data_version_id],
                ).fetchone()
                if collision and any(bool(value) for value in collision):
                    raise RecipeIdentifierConfusionError(
                        "DataVersion ID collides with an existing identity"
                    )
                next_number = int(
                    connection.execute(
                        """
                        SELECT coalesce(max(version_number), 0) + 1
                          FROM data_version WHERE recipe_id = ?
                        """,
                        [intent.recipe_id],
                    ).fetchone()[0]
                )
                parent_id = str(recipe_row[1]) if recipe_row[1] else None
                now = str(detail["created_at"])
                connection.execute(
                    """
                    INSERT INTO data_version (
                        data_version_id, recipe_id, version_number,
                        workspace_project_id, parent_data_version_id, purpose,
                        state, pinned_recipe_revision, label, export_as_of_date,
                        parameter_values_hash, created_at, sealed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, NULL)
                    """,
                    [
                        data_version_id,
                        intent.recipe_id,
                        next_number,
                        workspace_project_id,
                        parent_id,
                        purpose.value,
                        pinned_recipe_revision,
                        str(detail["label"]),
                        detail.get("export_as_of_date"),
                        detail.get("parameter_values_hash"),
                        now,
                    ],
                )
                if parent_id is not None:
                    connection.execute(
                        """
                        UPDATE data_version
                           SET state = 'SEALED', sealed_at = ?
                         WHERE data_version_id = ? AND state = 'ACTIVE'
                        """,
                        [now, parent_id],
                    )
                connection.execute(
                    """
                    UPDATE recipe
                       SET current_data_version_id = ?,
                           optimistic_revision = ?, updated_at = ?
                     WHERE recipe_id = ?
                    """,
                    [
                        data_version_id,
                        intent.expected_recipe_revision + 1,
                        now,
                        intent.recipe_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE recipe_intent
                       SET state = 'REGISTRY_COMMITTED', updated_at = ?
                     WHERE operation_id = ?
                    """,
                    [now, operation_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_intent(operation_id)

    def synchronize_workspace_markers(self, recipe_id: str) -> None:
        """Repair exact local linkage/seal markers from registry authority."""

        for data_version in self.data_versions(recipe_id):
            database_path = (
                self.project_directory(data_version.workspace_project_id)
                / "project.duckdb"
            )
            if not database_path.is_file():
                raise ProjectNotFoundError("Workspace project not found")
            with self._connect(database_path) as connection:
                self._ensure_project_database_schema(connection)
                current = connection.execute(
                    """
                    SELECT recipe_id, data_version_id, data_version_number
                      FROM recipe_workspace_linkage WHERE singleton_id = 1
                    """
                ).fetchone()
                expected = (
                    data_version.recipe_id,
                    data_version.data_version_id,
                    data_version.version_number,
                )
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO recipe_workspace_linkage VALUES (1, ?, ?, ?, ?)
                        """,
                        [*expected, datetime.now(timezone.utc).isoformat()],
                    )
                elif (str(current[0]), str(current[1]), int(current[2])) != expected:
                    prior_recipe_id = str(current[0])
                    prior_data_version_id = str(current[1])
                    with self._connect(self.registry_path) as registry:
                        prior_exists = registry.execute(
                            """
                            SELECT
                                EXISTS (SELECT 1 FROM recipe WHERE recipe_id = ?),
                                EXISTS (
                                    SELECT 1 FROM data_version
                                     WHERE data_version_id = ?
                                )
                            """,
                            [prior_recipe_id, prior_data_version_id],
                        ).fetchone()
                    if prior_exists and any(bool(value) for value in prior_exists):
                        raise ProjectError(
                            "Workspace Recipe/DataVersion linkage is inconsistent"
                        )
                    connection.execute(
                        """
                        UPDATE recipe_workspace_linkage
                           SET recipe_id = ?, data_version_id = ?,
                               data_version_number = ?, linked_at = ?
                         WHERE singleton_id = 1
                        """,
                        [*expected, datetime.now(timezone.utc).isoformat()],
                    )
                    connection.execute("DELETE FROM recipe_workspace_seal")
                if data_version.state is DataVersionState.SEALED:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO recipe_workspace_seal
                        VALUES (1, ?, 'DATA_VERSION_SEALED')
                        """,
                        [
                            (
                                data_version.sealed_at or datetime.now(timezone.utc)
                            ).isoformat()
                        ],
                    )

    def record_application_projection(
        self,
        *,
        application_id: str,
        recipe_id: str,
        recipe_revision: int,
        data_version_id: str,
        workspace_project_id: str,
        source_selection_hash: str,
        parameter_values_hash: str,
        target_binding_hash: str,
        credential_generation: str,
        binding_hash: str,
        issue_hash: str,
        mapping_id: str | None,
        mapping_content_hash: str | None,
        status: str,
        evidence_storage_key: str,
        evidence_hash: str,
        created_at: datetime,
    ) -> None:
        """Persist one bounded application projection for later qualification."""

        for value, name in (
            (application_id, "application_id"),
            (recipe_id, "recipe_id"),
            (data_version_id, "data_version_id"),
            (workspace_project_id, "workspace_project_id"),
        ):
            require_uuid(value, name)
        for value, name in (
            (source_selection_hash, "source_selection_hash"),
            (parameter_values_hash, "parameter_values_hash"),
            (target_binding_hash, "target_binding_hash"),
            (binding_hash, "binding_hash"),
            (issue_hash, "issue_hash"),
            (evidence_hash, "evidence_hash"),
        ):
            require_hash(value, name)
        if mapping_content_hash is not None:
            require_hash(mapping_content_hash, "mapping_content_hash")
        if created_at.tzinfo is None:
            raise RecipeConflictError("Application time must be timezone-aware")
        if status not in {"APPLIED", "BLOCKED"}:
            raise RecipeConflictError("Application status is invalid")
        with self._connect(self.registry_path) as connection:
            relation = connection.execute(
                """
                SELECT r.current_recipe_revision, d.workspace_project_id
                  FROM recipe r
                  JOIN data_version d ON d.recipe_id = r.recipe_id
                 WHERE r.recipe_id = ? AND d.data_version_id = ?
                """,
                [recipe_id, data_version_id],
            ).fetchone()
            if (
                relation is None
                or int(relation[0]) != recipe_revision
                or str(relation[1]) != workspace_project_id
            ):
                raise RecipeConflictError("Application lineage is invalid")
            connection.execute(
                """
                INSERT INTO recipe_application VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    application_id,
                    recipe_id,
                    recipe_revision,
                    data_version_id,
                    workspace_project_id,
                    source_selection_hash,
                    parameter_values_hash,
                    target_binding_hash,
                    credential_generation.strip(),
                    binding_hash,
                    issue_hash,
                    mapping_id,
                    mapping_content_hash,
                    status,
                    evidence_storage_key,
                    evidence_hash,
                    created_at.isoformat(),
                ],
            )

    def commit_qualification(self, operation_id: str) -> RecipeIntent:
        """Publish one protected Test qualification projection."""

        intent = self.get_intent(operation_id)
        if intent.kind is not RecipeIntentKind.QUALIFICATION_PUBLICATION:
            raise RecipeConflictError("Recipe operation kind is invalid")
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            return intent
        if intent.state is not RecipeIntentState.PAYLOAD_STORED:
            raise RecipeConflictError("Qualification evidence is not stored")
        detail = intent.detail
        actor = self._actor_detail(detail)
        hash_fields = (
            "test_target_binding_hash",
            "preparation_hash",
            "quality_hash",
            "control_hash",
            "comparison_hash",
            "execution_hash",
            "read_back_hash",
            "reconciliation_hash",
            "evidence_hash",
        )
        for field in hash_fields:
            require_hash(str(detail[field]), field)
        application_evidence_hash = require_hash(
            str(detail["application_evidence_hash"]),
            "application_evidence_hash",
        )
        with self._connect(self.registry_path) as connection:
            connection.begin()
            try:
                recipe = connection.execute(
                    """
                    SELECT current_recipe_revision, optimistic_revision
                      FROM recipe WHERE recipe_id = ?
                    """,
                    [intent.recipe_id],
                ).fetchone()
                if recipe is None or int(recipe[1]) != intent.expected_recipe_revision:
                    raise RecipeConflictError("Recipe changed before qualification")
                if int(recipe[0]) != int(detail["recipe_revision"]):
                    raise RecipeConflictError("Qualification is not for current Recipe")
                application = connection.execute(
                    """
                    SELECT recipe_revision, target_binding_hash, status,
                           evidence_hash, data_version_id, workspace_project_id
                      FROM recipe_application
                     WHERE application_id = ? AND recipe_id = ?
                    """,
                    [str(detail["application_id"]), intent.recipe_id],
                ).fetchone()
                if (
                    application is None
                    or int(application[0]) != int(detail["recipe_revision"])
                    or str(application[1]) != str(detail["test_target_binding_hash"])
                    or str(application[2]) != "APPLIED"
                    or str(application[3]) != application_evidence_hash
                    or str(application[4]) != str(detail["data_version_id"])
                    or str(application[5]) != str(detail["workspace_project_id"])
                ):
                    raise RecipeConflictError(
                        "Qualification application evidence is unavailable"
                    )
                now = str(detail["qualified_at"])
                connection.execute(
                    """
                    INSERT INTO recipe_qualification VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        str(detail["qualification_id"]),
                        intent.recipe_id,
                        int(detail["recipe_revision"]),
                        str(detail["application_id"]),
                        str(detail["test_target_binding_hash"]),
                        str(detail["preparation_hash"]),
                        str(detail["quality_hash"]),
                        str(detail["control_hash"]),
                        str(detail["comparison_hash"]),
                        str(detail["execution_hash"]),
                        str(detail["read_back_hash"]),
                        str(detail["reconciliation_hash"]),
                        "TEST_QUALIFIED",
                        canonical_json(detail.get("findings", [])),
                        actor.issuer,
                        actor.subject_id,
                        actor.display_name,
                        now,
                        str(detail["storage_key"]),
                        str(detail["evidence_hash"]),
                    ],
                )
                connection.execute(
                    """
                    UPDATE recipe SET optimistic_revision = ?, updated_at = ?
                     WHERE recipe_id = ?
                    """,
                    [intent.expected_recipe_revision + 1, now, intent.recipe_id],
                )
                connection.execute(
                    """
                    UPDATE recipe_intent
                       SET state = 'REGISTRY_COMMITTED', updated_at = ?
                     WHERE operation_id = ?
                    """,
                    [now, operation_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_intent(operation_id)

    def commit_cutover(self, operation_id: str) -> RecipeIntent:
        """Select one exact qualified revision under Recipe optimism."""

        intent = self.get_intent(operation_id)
        if intent.kind is not RecipeIntentKind.CUTOVER_SELECTION:
            raise RecipeConflictError("Recipe operation kind is invalid")
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            return intent
        detail = intent.detail
        actor = self._actor_detail(detail)
        with self._connect(self.registry_path) as connection:
            connection.begin()
            try:
                recipe = connection.execute(
                    """
                    SELECT current_recipe_revision, optimistic_revision
                      FROM recipe WHERE recipe_id = ?
                    """,
                    [intent.recipe_id],
                ).fetchone()
                if recipe is None or int(recipe[1]) != intent.expected_recipe_revision:
                    raise RecipeConflictError("Recipe changed before cutover selection")
                if int(recipe[0]) != int(detail["recipe_revision"]):
                    raise RecipeConflictError(
                        "Only the current Recipe revision can be selected"
                    )
                qualification = connection.execute(
                    """
                    SELECT recipe_revision, evidence_hash, status
                      FROM recipe_qualification
                     WHERE qualification_id = ? AND recipe_id = ?
                    """,
                    [str(detail["qualification_id"]), intent.recipe_id],
                ).fetchone()
                if (
                    qualification is None
                    or str(qualification[2]) != "TEST_QUALIFIED"
                    or int(qualification[0]) != int(detail["recipe_revision"])
                    or str(qualification[1])
                    != str(detail["qualification_evidence_hash"])
                ):
                    raise RecipeConflictError("Exact Test qualification is unavailable")
                now = str(detail["selected_at"])
                connection.execute(
                    """
                    INSERT INTO cutover_candidate VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        str(detail["cutover_candidate_id"]),
                        intent.recipe_id,
                        int(detail["recipe_revision"]),
                        str(detail["qualification_id"]),
                        intent.expected_recipe_revision,
                        actor.issuer,
                        actor.subject_id,
                        actor.display_name,
                        now,
                        str(detail["content_hash"]),
                    ],
                )
                connection.execute(
                    """
                    UPDATE recipe
                       SET cutover_candidate_id = ?, optimistic_revision = ?,
                           updated_at = ?
                     WHERE recipe_id = ?
                    """,
                    [
                        str(detail["cutover_candidate_id"]),
                        intent.expected_recipe_revision + 1,
                        now,
                        intent.recipe_id,
                    ],
                )
                connection.execute(
                    """
                    UPDATE recipe_intent
                       SET state = 'REGISTRY_COMMITTED', updated_at = ?
                     WHERE operation_id = ?
                    """,
                    [now, operation_id],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_intent(operation_id)

    @staticmethod
    def _actor_detail(detail: Mapping[str, object]) -> ActorIdentity:
        actor = detail.get("actor")
        if not isinstance(actor, Mapping):
            raise RecipeConflictError("Recipe operation actor is missing")
        return ActorIdentity(
            issuer=str(actor.get("issuer", "")),
            subject_id=str(actor.get("subject_id", "")),
            display_name=str(actor.get("display_name", "")),
        )

    @staticmethod
    def _recipe(row: Mapping[str, object]) -> Recipe:
        return Recipe(
            recipe_id=str(row["recipe_id"]),
            display_name=str(row["display_name"]),
            business_purpose=str(row["business_purpose"]),
            data_classification=str(row["data_classification"]),
            retention_days=int(row["retention_days"]),
            current_recipe_revision=(
                int(row["current_recipe_revision"])
                if row["current_recipe_revision"] is not None
                else None
            ),
            current_data_version_id=(
                str(row["current_data_version_id"])
                if row["current_data_version_id"]
                else None
            ),
            cutover_candidate_id=(
                str(row["cutover_candidate_id"])
                if row["cutover_candidate_id"]
                else None
            ),
            optimistic_revision=int(row["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _data_version(row: Mapping[str, object]) -> DataVersion:
        return DataVersion(
            data_version_id=str(row["data_version_id"]),
            recipe_id=str(row["recipe_id"]),
            version_number=int(row["version_number"]),
            workspace_project_id=str(row["workspace_project_id"]),
            parent_data_version_id=(
                str(row["parent_data_version_id"])
                if row["parent_data_version_id"]
                else None
            ),
            purpose=DataVersionPurpose(str(row["purpose"])),
            state=DataVersionState(str(row["state"])),
            pinned_recipe_revision=(
                int(row["pinned_recipe_revision"])
                if row["pinned_recipe_revision"] is not None
                else None
            ),
            label=str(row["label"]),
            export_as_of_date=(
                date.fromisoformat(str(row["export_as_of_date"]))
                if row["export_as_of_date"]
                else None
            ),
            parameter_values_hash=(
                str(row["parameter_values_hash"])
                if row["parameter_values_hash"]
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            sealed_at=(
                datetime.fromisoformat(str(row["sealed_at"]))
                if row["sealed_at"]
                else None
            ),
        )

    @staticmethod
    def _intent(row: Mapping[str, object]) -> RecipeIntent:
        return RecipeIntent(
            operation_id=str(row["operation_id"]),
            recipe_id=str(row["recipe_id"]),
            kind=RecipeIntentKind(str(row["kind"])),
            state=RecipeIntentState(str(row["state"])),
            expected_recipe_revision=int(row["expected_recipe_revision"]),
            detail=json.loads(str(row["detail_json"])),
            last_error=str(row["last_error"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
