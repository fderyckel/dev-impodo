"""Define Project-scoped reusable Recipe identity and immutable revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from ...migration_foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)


class RecipeError(MigrationFoundationError):
    """Reject invalid Recipe work without changing Project-owned evidence."""


class RecipeConflictError(RecipeError):
    """Reject conflicting or non-portable reusable meaning."""


class RecipeIntegrityError(RecipeError):
    """Reject stored or compiled Recipe meaning that fails verification."""


class RecipeDraftRecoveryStep(StrEnum):
    """Name the workspace stage that owns one publication blocker."""

    SOURCE_DATA = "source-data"
    ODOO_DATA = "odoo-data"
    MATCH_DATA = "match-data"
    PREPARE_DATA = "prepare-data"
    NEW_PROJECT = "new-project"


@dataclass(frozen=True, slots=True)
class RecipeDraftIssue:
    """Explain why current workspace meaning cannot be published yet."""

    code: str
    message: str
    recovery_action: str
    recovery_step: RecipeDraftRecoveryStep
    logical_id: str = ""


@dataclass(frozen=True, slots=True)
class Recipe:
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
class RecipeRevision:
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
                raise RecipeError("Recipe revision lineage is invalid")
        require_hash(self.semantic_hash, "semantic_hash")
        require_hash(self.payload_hash, "payload_hash")
        required_text(self.storage_key, "storage_key", maximum=1_000)
        require_hash(self.artifact_hash, "artifact_hash")
        require_aware(self.published_at, "published_at")


@dataclass(frozen=True, slots=True)
class RecipePublication:
    """Return the Recipe identity and exact revision created by one command."""

    recipe: Recipe
    revision: RecipeRevision


@dataclass(frozen=True, slots=True)
class RecipeRevisionRead:
    """Return one exact Recipe identity, revision, and verified definition."""

    recipe: Recipe
    revision: RecipeRevision
    envelope: Mapping[str, object]

