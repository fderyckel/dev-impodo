"""Define Recipe aggregate, DataVersion lineage, and recovery contracts.

Recipe is the reusable business identity. Each DataVersion owns one existing
contained ``WorkspaceState`` workspace. The types here deliberately contain
no DuckDB, filesystem, web, or Odoo transport behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import re
from typing import Mapping, Protocol
from uuid import UUID

from .access import ActorIdentity


_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RecipeError(ValueError):
    """Base error for invalid Recipe operations."""


class RecipeNotFoundError(RecipeError):
    """Raised when an exact Recipe identity does not exist."""


class RecipeConflictError(RecipeError):
    """Raised when optimistic state changed before a command committed."""


class RecipeIdentifierConfusionError(RecipeError):
    """Raised when one Recipe identity namespace is used as another."""


class RecipeIntegrityError(RecipeError):
    """Raised when protected Recipe content or hashes do not verify."""


class DataVersionPurpose(StrEnum):
    AUTHORING = "AUTHORING"
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class DataVersionState(StrEnum):
    ACTIVE = "ACTIVE"
    SEALED = "SEALED"


class RecipeIntentKind(StrEnum):
    RECIPE_PUBLICATION = "RECIPE_PUBLICATION"
    DATA_VERSION_CREATION = "DATA_VERSION_CREATION"
    QUALIFICATION_PUBLICATION = "QUALIFICATION_PUBLICATION"
    CUTOVER_SELECTION = "CUTOVER_SELECTION"


class RecipeIntentState(StrEnum):
    RESERVED = "RESERVED"
    PAYLOAD_STORED = "PAYLOAD_STORED"
    REGISTRY_COMMITTED = "REGISTRY_COMMITTED"
    COMPLETE = "COMPLETE"
    ABANDONED = "ABANDONED"


class RecipeDraftState(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class RecipeDraftRecoveryStep(StrEnum):
    """Name the workflow surface that owns one publication recovery."""

    PROJECT_SETUP = "PROJECT_SETUP"
    SOURCE_DATA = "SOURCE_DATA"
    ODOO_DATA = "ODOO_DATA"
    MATCH_DATA = "MATCH_DATA"
    PREPARE_DATA = "PREPARE_DATA"
    RECIPE_OVERVIEW = "RECIPE_OVERVIEW"
    RECIPE_APPLICATION = "RECIPE_APPLICATION"
    NEW_PROJECT = "NEW_PROJECT"


@dataclass(frozen=True, slots=True)
class RecipeDraftIssue:
    """Explain one publication blocker and its single recovery action."""

    code: str
    message: str
    recovery_action: str
    recovery_step: RecipeDraftRecoveryStep
    support_reference: str = ""


@dataclass(frozen=True, slots=True)
class RecipeDraft:
    """Project current authoring evidence without duplicating mutable drafts."""

    recipe_id: str
    data_version_id: str
    workspace_project_id: str
    state: RecipeDraftState
    expected_recipe_revision: int
    next_recipe_revision: int
    semantic_hash: str | None
    source_selection_hash: str | None
    mapping_content_hash: str | None
    schema_hash: str | None
    quality_ruleset_hash: str | None
    issues: tuple[RecipeDraftIssue, ...]

    @property
    def can_publish(self) -> bool:
        return self.state is RecipeDraftState.READY and not self.issues


@dataclass(frozen=True, slots=True)
class Recipe:
    recipe_id: str
    display_name: str
    business_purpose: str
    data_classification: str
    retention_days: int
    current_recipe_revision: int | None
    current_data_version_id: str | None
    cutover_candidate_id: str | None
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.recipe_id, "recipe_id")
        for value, name in (
            (self.current_data_version_id, "current_data_version_id"),
            (self.cutover_candidate_id, "cutover_candidate_id"),
        ):
            if value is not None:
                _uuid(value, name)
        if not self.display_name.strip() or len(self.display_name) > 200:
            raise RecipeError("Recipe name is invalid")
        if not 1 <= self.retention_days <= 3650:
            raise RecipeError("Recipe retention is invalid")
        if self.optimistic_revision < 1:
            raise RecipeError("Recipe revision is invalid")


@dataclass(frozen=True, slots=True)
class RecipeSummary:
    recipe_id: str
    display_name: str
    current_recipe_revision: int | None
    current_data_version_id: str | None
    current_workspace_project_id: str | None
    current_workspace_revision: int | None
    data_version_count: int
    deletable: bool
    qualification_status: str | None
    cutover_recipe_revision: int | None
    optimistic_revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DataVersion:
    data_version_id: str
    recipe_id: str
    version_number: int
    workspace_project_id: str
    parent_data_version_id: str | None
    purpose: DataVersionPurpose
    state: DataVersionState
    pinned_recipe_revision: int | None
    label: str
    export_as_of_date: date | None
    parameter_values_hash: str | None
    created_at: datetime
    sealed_at: datetime | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.data_version_id, "data_version_id"),
            (self.recipe_id, "recipe_id"),
            (self.workspace_project_id, "workspace_project_id"),
        ):
            _uuid(value, name)
        if self.parent_data_version_id is not None:
            _uuid(self.parent_data_version_id, "parent_data_version_id")
        object.__setattr__(self, "purpose", DataVersionPurpose(self.purpose))
        object.__setattr__(self, "state", DataVersionState(self.state))
        if self.version_number < 1:
            raise RecipeError("Data version number is invalid")
        if self.pinned_recipe_revision is not None and self.pinned_recipe_revision < 1:
            raise RecipeError("Pinned Recipe revision is invalid")
        if self.parameter_values_hash is not None:
            _hash(self.parameter_values_hash, "parameter_values_hash")


@dataclass(frozen=True, slots=True)
class WorkspaceResolution:
    recipe_id: str
    data_version_id: str
    data_version_number: int
    workspace_project_id: str
    data_version_purpose: DataVersionPurpose
    data_version_state: DataVersionState


@dataclass(frozen=True, slots=True)
class RecipeRevision:
    recipe_id: str
    version: int
    parent_version: int | None
    semantic_hash: str
    payload_hash: str
    storage_key: str
    artifact_hash: str
    size_bytes: int
    contract_versions: Mapping[str, int]
    provenance: Mapping[str, object]
    published_by: ActorIdentity
    published_at: datetime


@dataclass(frozen=True, slots=True)
class RecipeIntent:
    operation_id: str
    recipe_id: str
    kind: RecipeIntentKind
    state: RecipeIntentState
    expected_recipe_revision: int
    detail: Mapping[str, object]
    last_error: str
    created_at: datetime
    updated_at: datetime


class RecipeRepository(Protocol):
    """Registry persistence required by Recipe application services."""

    def list(self) -> tuple[RecipeSummary, ...]: ...
    def get(self, recipe_id: str) -> Recipe: ...
    def data_versions(self, recipe_id: str) -> tuple[DataVersion, ...]: ...
    def resolve_workspace(self, workspace_project_id: str) -> WorkspaceResolution: ...


def require_hash(value: str, name: str) -> str:
    """Validate and return a canonical SHA-256 content identifier."""

    return _hash(value, name)


def require_uuid(value: str, name: str) -> str:
    """Validate and return a canonical UUID string."""

    return _uuid(value, name)


def _hash(value: str, name: str) -> str:
    if _HASH.fullmatch(value) is None:
        raise RecipeError(f"{name} is invalid")
    return value


def _uuid(value: str, name: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise RecipeError(f"{name} is invalid") from error
    if canonical != value:
        raise RecipeError(f"{name} is invalid")
    return value
