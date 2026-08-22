"""Define Project-owned reusable Recipe identities and immutable revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from .access import Actor, AuthorizationPolicy, Capability
from .migration_foundation import (
    FaultInjector,
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)


class ProjectRecipeError(MigrationFoundationError):
    """Reject invalid publication without changing Project-owned evidence."""


@dataclass(frozen=True, slots=True)
class ProjectRecipe:
    """Identify one reusable transformation purpose inside a Project."""

    recipe_id: str
    project_id: str
    display_name: str
    business_purpose: str
    current_recipe_revision: int
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        require_uuid(self.project_id, "project_id")
        object.__setattr__(
            self,
            "display_name",
            required_text(self.display_name, "display_name", maximum=200),
        )
        object.__setattr__(
            self,
            "business_purpose",
            required_text(
                self.business_purpose,
                "business_purpose",
                maximum=2_000,
            ),
        )
        require_revision(
            self.current_recipe_revision,
            "current_recipe_revision",
        )
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.archived_at is not None:
            require_aware(self.archived_at, "archived_at")


@dataclass(frozen=True, slots=True)
class ProjectRecipeRevision:
    """Reference one authenticated immutable Recipe envelope."""

    recipe_id: str
    version: int
    parent_version: int | None
    semantic_hash: str
    payload_hash: str
    storage_key: str
    artifact_hash: str
    contract_versions: Mapping[str, object]
    provenance: Mapping[str, object]
    published_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        require_revision(self.version, "version")
        if self.parent_version is not None:
            require_revision(self.parent_version, "parent_version")
            if self.parent_version >= self.version:
                raise ProjectRecipeError("Recipe revision lineage is invalid")
        require_hash(self.semantic_hash, "semantic_hash")
        require_hash(self.payload_hash, "payload_hash")
        required_text(self.storage_key, "storage_key", maximum=1_000)
        require_hash(self.artifact_hash, "artifact_hash")
        require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class RecipePublication:
    """Return the Recipe identity and exact revision created by one command."""

    recipe: ProjectRecipe
    revision: ProjectRecipeRevision


class ProjectRecipeRepository(Protocol):
    def get_recipe(self, recipe_id: str) -> ProjectRecipe: ...

    def list_recipes(self, project_id: str) -> tuple[ProjectRecipe, ...]: ...

    def list_recipe_revisions(
        self,
        recipe_id: str,
    ) -> tuple[ProjectRecipeRevision, ...]: ...

    def read_recipe_revision(
        self,
        recipe_id: str,
        version: int,
    ) -> Mapping[str, object]: ...

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
    ) -> RecipePublication: ...


class ProjectRecipeService:
    """Authorize Project-scoped Recipe reads and publication."""

    def __init__(
        self,
        repository: ProjectRecipeRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def get(self, recipe_id: str, *, actor: Actor) -> ProjectRecipe:
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        recipe = self.repository.get_recipe(require_uuid(recipe_id, "recipe_id"))
        self.authorization.require(
            actor,
            Capability.RECIPE_VIEW,
            project_id=recipe.project_id,
        )
        return recipe

    def list(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[ProjectRecipe, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.list_recipes(project_id)

    def revisions(
        self,
        recipe_id: str,
        *,
        actor: Actor,
    ) -> tuple[ProjectRecipeRevision, ...]:
        self.get(recipe_id, actor=actor)
        return self.repository.list_recipe_revisions(recipe_id)

    def read_revision(
        self,
        recipe_id: str,
        version: int,
        *,
        actor: Actor,
    ) -> Mapping[str, object]:
        self.get(recipe_id, actor=actor)
        return self.repository.read_recipe_revision(
            recipe_id,
            require_revision(version, "version"),
        )

