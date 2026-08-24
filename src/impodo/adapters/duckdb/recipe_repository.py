"""Publish Project-owned Recipe revisions across registry and protected storage."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Mapping
from uuid import uuid4

from ...access import Actor
from ...adapters.protected_recipe_store import ProtectedRecipeStore
from ...domain.serialization import canonical_json, content_hash
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ...recipes import (
    Recipe,
    RecipeError,
    RecipeRevision,
    RecipePublication,
)
from .migration_foundation_repository import MigrationFoundationRepository


class RecipeRepository:
    """Own restart-safe Recipe publication without owning its DataVersion."""

    def __init__(
        self,
        foundation: MigrationFoundationRepository,
        store: ProtectedRecipeStore,
    ) -> None:
        self.foundation = foundation
        self.database = foundation.database
        self.store = store

    def get_recipe(self, recipe_id: str) -> Recipe:
        recipe_id = require_uuid(recipe_id, "recipe_id")
        with self.database.connect(self.foundation.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe WHERE recipe_id = ?",
                [recipe_id],
            )
            if not rows:
                self.foundation._raise_missing_identity(connection, recipe_id)
        return self._recipe_from_row(rows[0])

    def list_recipes(self, project_id: str) -> tuple[Recipe, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.foundation.registry_path) as connection:
            self.foundation._require_project(connection, project_id)
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe WHERE project_id = ? AND archived_at IS NULL "
                "ORDER BY updated_at DESC, recipe_id",
                [project_id],
            )
        return tuple(self._recipe_from_row(row) for row in rows)

    def list_recipe_revisions(
        self,
        recipe_id: str,
    ) -> tuple[RecipeRevision, ...]:
        recipe_id = require_uuid(recipe_id, "recipe_id")
        self.get_recipe(recipe_id)
        with self.database.connect(self.foundation.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_revision WHERE recipe_id = ? "
                "ORDER BY version",
                [recipe_id],
            )
        return tuple(self._revision_from_row(row) for row in rows)

    def read_recipe_revision(
        self,
        recipe_id: str,
        version: int,
    ) -> Mapping[str, object]:
        recipe_id = require_uuid(recipe_id, "recipe_id")
        version = require_revision(version, "version")
        with self.database.connect(self.foundation.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_revision WHERE recipe_id = ? AND version = ?",
                [recipe_id, version],
            )
        if not rows:
            self.get_recipe(recipe_id)
            raise MigrationNotFoundError("Recipe revision not found")
        revision = self._revision_from_row(rows[0])
        payload = self.store.read(
            recipe_id,
            storage_key=revision.storage_key,
            logical_hash=revision.payload_hash,
            expected_artifact_hash=revision.artifact_hash,
        )
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipeError("Stored Recipe envelope is invalid") from error
        if not isinstance(envelope, dict):
            raise RecipeError("Stored Recipe envelope is invalid")
        claimed = envelope.pop("payload_hash", None)
        if claimed != revision.payload_hash or content_hash(envelope) != claimed:
            raise RecipeError("Stored Recipe payload hash is inconsistent")
        envelope["payload_hash"] = claimed
        if envelope.get("semantic_hash") != revision.semantic_hash:
            raise RecipeError("Stored Recipe semantic hash is inconsistent")
        return envelope

    def publish_recipe(
        self,
        *,
        project_id: str,
        data_version_id: str,
        workspace_id: str,
        recipe_id: str | None,
        expected_recipe_revision: int | None,
        display_name: str,
        business_purpose: str,
        compiled_recipe: Mapping[str, object],
        compatibility_hints: Mapping[str, object],
        compilation_provenance: Mapping[str, object],
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> RecipePublication:
        """Create one identity/revision transaction after durable payload storage."""

        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        operation_id = require_uuid(operation_id, "operation_id")
        display_name = required_text(display_name, "display_name", maximum=200)
        business_purpose = required_text(
            business_purpose,
            "business_purpose",
            maximum=2_000,
        )
        if recipe_id is not None:
            recipe_id = require_uuid(recipe_id, "recipe_id")
            if expected_recipe_revision is None:
                raise RecipeError(
                    "A successor publication requires the current Recipe revision"
                )
            expected_recipe_revision = require_revision(
                expected_recipe_revision,
                "expected_recipe_revision",
            )
        elif expected_recipe_revision is not None:
            raise RecipeError(
                "A first Recipe publication has no existing Recipe revision"
            )

        semantic_hash = content_hash(compiled_recipe)
        with self.database.connect(self.foundation.registry_path) as connection:
            version = self._validate_publication_context(
                connection,
                project_id=project_id,
                data_version_id=data_version_id,
                workspace_id=workspace_id,
                recipe_id=recipe_id,
                expected_recipe_revision=expected_recipe_revision,
            )
            if recipe_id is not None:
                existing = connection.execute(
                    "SELECT version FROM recipe_revision "
                    "WHERE recipe_id = ? AND semantic_hash = ?",
                    [recipe_id, semantic_hash],
                ).fetchone()
                if existing is not None:
                    return self._publication(recipe_id, int(existing[0]))

        proposed_recipe_id = recipe_id or str(uuid4())
        published_at = utc_now()
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=project_id,
            owner_kind="RECIPE",
            owner_id=proposed_recipe_id,
            kind=MigrationOperationKind.RECIPE_PUBLISH,
            request_hash=request_hash,
            expected_revision=expected_recipe_revision,
            detail={
                "business_purpose": business_purpose,
                "data_version_id": data_version_id,
                "display_name": display_name,
                "published_at": published_at.isoformat(),
                "version": version,
                "workspace_id": workspace_id,
            },
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self._publication(intent.owner_id, int(intent.detail["version"]))

        stored_recipe_id = intent.owner_id
        stored_version = int(intent.detail["version"])
        stored_published_at = datetime.fromisoformat(
            str(intent.detail["published_at"])
        )
        provenance = {
            **dict(compilation_provenance),
            "compiled_at": stored_published_at.isoformat(),
            "origin_data_version_id": data_version_id,
            "origin_project_id": project_id,
            "origin_workspace_id": workspace_id,
            "publisher": {
                "display_name": actor.identity.display_name,
                "issuer": actor.identity.issuer,
                "subject_id": actor.identity.subject_id,
            },
            "recipe_id": stored_recipe_id,
            "recipe_revision": stored_version,
        }
        envelope: dict[str, object] = {
            "recipe_contract_version": 2,
            "semantic_hash": semantic_hash,
            "recipe": dict(compiled_recipe),
            "compatibility_hints": dict(compatibility_hints),
            "provenance": provenance,
        }
        payload_hash = content_hash(envelope)
        envelope["payload_hash"] = payload_hash
        payload = canonical_json(envelope).encode("utf-8")

        self.foundation._fault(fault, "INTENT_RESERVED")
        stored_payload = self.store.put(
            stored_recipe_id,
            kind="revisions",
            object_id=f"v{stored_version}",
            logical_hash=payload_hash,
            payload=payload,
        )
        self.foundation._fault(fault, "ARTIFACT_STORED")

        with self.database.connect(self.foundation.registry_path) as connection:
            connection.begin()
            try:
                self._validate_publication_context(
                    connection,
                    project_id=project_id,
                    data_version_id=data_version_id,
                    workspace_id=workspace_id,
                    recipe_id=(stored_recipe_id if recipe_id is not None else None),
                    expected_recipe_revision=expected_recipe_revision,
                )
                if recipe_id is None:
                    self.foundation._assert_identity_available(
                        connection,
                        stored_recipe_id,
                    )
                    connection.execute(
                        "INSERT INTO recipe_identity VALUES (?)",
                        [stored_recipe_id],
                    )
                    connection.execute(
                        "INSERT INTO recipe VALUES (?, ?, ?, ?, 1, 1, ?, ?, NULL)",
                        [
                            stored_recipe_id,
                            project_id,
                            str(intent.detail["display_name"]),
                            str(intent.detail["business_purpose"]),
                            stored_published_at.isoformat(),
                            stored_published_at.isoformat(),
                        ],
                    )
                else:
                    updated = connection.execute(
                        """
                        UPDATE recipe
                           SET display_name = ?, business_purpose = ?,
                               current_recipe_revision = ?,
                               optimistic_revision = optimistic_revision + 1,
                               updated_at = ?
                         WHERE recipe_id = ? AND project_id = ?
                           AND optimistic_revision = ?
                        RETURNING recipe_id
                        """,
                        [
                            str(intent.detail["display_name"]),
                            str(intent.detail["business_purpose"]),
                            stored_version,
                            stored_published_at.isoformat(),
                            stored_recipe_id,
                            project_id,
                            expected_recipe_revision,
                        ],
                    ).fetchone()
                    if updated is None:
                        raise MigrationConflictError(
                            "Recipe changed; reload before publishing"
                        )
                connection.execute(
                    """
                    INSERT INTO recipe_revision VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        stored_recipe_id,
                        stored_version,
                        stored_version - 1 if stored_version > 1 else None,
                        semantic_hash,
                        payload_hash,
                        stored_payload.storage_key,
                        stored_payload.artifact_hash,
                        canonical_json(
                            dict(compiled_recipe).get("contract_versions", {})
                        ),
                        canonical_json(provenance),
                        stored_published_at.isoformat(),
                    ],
                )
                self.foundation._insert_event(
                    connection,
                    project_id=project_id,
                    aggregate_kind="RECIPE",
                    aggregate_id=stored_recipe_id,
                    aggregate_revision=(
                        1
                        if expected_recipe_revision is None
                        else expected_recipe_revision + 1
                    ),
                    event_type="RECIPE_REVISION_PUBLISHED",
                    detail={
                        "data_version_id": data_version_id,
                        "semantic_hash": semantic_hash,
                        "version": stored_version,
                        "workspace_id": workspace_id,
                    },
                    actor=actor,
                    occurred_at=stored_published_at,
                )
                self.foundation._commit_intent(
                    connection,
                    operation_id,
                    stage="REGISTRY_COMMITTED",
                    result={
                        "recipe_id": stored_recipe_id,
                        "version": stored_version,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.foundation._fault(fault, "REGISTRY_COMMITTED")
        return self._publication(stored_recipe_id, stored_version)

    def _validate_publication_context(
        self,
        connection,
        *,
        project_id: str,
        data_version_id: str,
        workspace_id: str,
        recipe_id: str | None,
        expected_recipe_revision: int | None,
    ) -> int:
        context = connection.execute(
            """
            SELECT w.project_id, w.data_version_id, w.recipe_application_id,
                   w.state, d.purpose, d.state, r.purpose
              FROM migration_workspace w
              JOIN data_version d ON d.data_version_id = w.data_version_id
              JOIN migration_run r ON r.migration_run_id = w.migration_run_id
             WHERE w.workspace_id = ?
            """,
            [workspace_id],
        ).fetchone()
        if context is None:
            self.foundation._raise_missing_identity(connection, workspace_id)
        if (str(context[0]), str(context[1])) != (project_id, data_version_id):
            raise RecipeError(
                "Recipe publication does not match this Project and DataVersion"
            )
        if context[2] is not None or tuple(str(item) for item in context[3:]) != (
            "OPEN",
            "AUTHORING",
            "FROZEN",
            "AUTHORING",
        ):
            raise RecipeError(
                "Only an open Authoring workspace can publish reusable rules"
            )
        if recipe_id is None:
            return 1
        row = connection.execute(
            "SELECT project_id, current_recipe_revision, optimistic_revision "
            "FROM recipe WHERE recipe_id = ? AND archived_at IS NULL",
            [recipe_id],
        ).fetchone()
        if row is None:
            self.foundation._raise_missing_identity(connection, recipe_id)
        if str(row[0]) != project_id:
            raise RecipeError("Recipe belongs to another Project")
        if int(row[2]) != expected_recipe_revision:
            raise MigrationConflictError("Recipe changed; reload before publishing")
        return int(row[1]) + 1

    def _publication(self, recipe_id: str, version: int) -> RecipePublication:
        recipe = self.get_recipe(recipe_id)
        revision = next(
            item
            for item in self.list_recipe_revisions(recipe_id)
            if item.version == version
        )
        return RecipePublication(recipe=recipe, revision=revision)

    @staticmethod
    def _recipe_from_row(row: Mapping[str, object]) -> Recipe:
        current = row.get("current_recipe_revision")
        if current is None:
            raise RecipeError("Stored Recipe has no published revision")
        return Recipe(
            recipe_id=str(row["recipe_id"]),
            project_id=str(row["project_id"]),
            display_name=str(row["display_name"]),
            business_purpose=str(row["business_purpose"]),
            current_recipe_revision=int(current),
            optimistic_revision=int(row["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            archived_at=(
                datetime.fromisoformat(str(row["archived_at"]))
                if row.get("archived_at")
                else None
            ),
        )

    @staticmethod
    def _revision_from_row(row: Mapping[str, object]) -> RecipeRevision:
        return RecipeRevision(
            recipe_id=str(row["recipe_id"]),
            version=int(row["version"]),
            parent_version=(
                int(row["parent_version"])
                if row.get("parent_version") is not None
                else None
            ),
            semantic_hash=str(row["semantic_hash"]),
            payload_hash=str(row["payload_hash"]),
            storage_key=str(row["storage_key"]),
            artifact_hash=str(row["artifact_hash"]),
            contract_versions=json.loads(str(row["contract_versions_json"])),
            provenance=json.loads(str(row["provenance_json"])),
            published_at=datetime.fromisoformat(str(row["published_at"])),
        )
