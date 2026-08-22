"""Define run-level planning for several Project-owned Recipe revisions.

M4 binds one exact Test DataVersion and one exact Odoo target to a
MigrationRun.  The run owns the unioned target requirements and dependency
order.  Each selected Recipe revision receives a separate application and
MigrationWorkspace; no mutable workspace state is shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from .domain.serialization import content_hash
from .migration_foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from .migration_runs import MigrationRun
from .migration_workspaces import MigrationWorkspace


MIGRATION_RUN_REQUIREMENT_PLAN_VERSION = 1


class MigrationRunPlanningError(MigrationFoundationError):
    """Reject an unsafe integrated run before application provisioning."""


class RecipeApplicationStatus(StrEnum):
    """Expose bounded progress for one isolated Recipe application."""

    DRAFT_READINESS = "DRAFT_READINESS"
    READY = "READY"
    BLOCKED = "BLOCKED"
    RUNNING = "RUNNING"
    PREPARED = "PREPARED"
    COMPARED = "COMPARED"
    EXECUTED = "EXECUTED"
    RECONCILED = "RECONCILED"
    FAILED = "FAILED"


class MigrationRunPlanIssueLevel(StrEnum):
    BLOCKER = "BLOCKER"
    REVIEW = "REVIEW"
    INFORMATION = "INFORMATION"


@dataclass(frozen=True, slots=True)
class MigrationRunPlanIssue:
    """Explain one bounded planning or application compatibility problem."""

    code: str
    level: MigrationRunPlanIssueLevel
    message: str
    recovery_action: str
    recipe_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", MigrationRunPlanIssueLevel(self.level))
        object.__setattr__(
            self,
            "code",
            required_text(self.code, "issue code", maximum=120),
        )
        object.__setattr__(
            self,
            "message",
            required_text(self.message, "issue message", maximum=1_000),
        )
        object.__setattr__(
            self,
            "recovery_action",
            required_text(
                self.recovery_action,
                "recovery action",
                maximum=1_000,
            ),
        )
        normalized = tuple(sorted(self.recipe_ids))
        for recipe_id in normalized:
            require_uuid(recipe_id, "recipe_id")
        if len(set(normalized)) != len(normalized):
            raise MigrationRunPlanningError("Issue Recipe identities are duplicated")
        object.__setattr__(self, "recipe_ids", normalized)

    @property
    def blocks(self) -> bool:
        return self.level is MigrationRunPlanIssueLevel.BLOCKER

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "level": self.level.value,
            "message": self.message,
            "recovery_action": self.recovery_action,
            "recipe_ids": list(self.recipe_ids),
        }


@dataclass(frozen=True, slots=True, order=True)
class RecipeRevisionSelection:
    """Pin one immutable Project Recipe revision for this run."""

    recipe_id: str
    recipe_revision: int
    semantic_hash: str

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        require_revision(self.recipe_revision, "recipe_revision")
        require_hash(self.semantic_hash, "semantic_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "recipe_revision": self.recipe_revision,
            "semantic_hash": self.semantic_hash,
        }


@dataclass(frozen=True, slots=True, order=True)
class RecipeDependency:
    """Require one Recipe application to reconcile before another begins."""

    before_recipe_id: str
    after_recipe_id: str
    kind: str = "PROJECT_SEQUENCE"
    reason: str = "The data manager selected this integrated run order."

    def __post_init__(self) -> None:
        require_uuid(self.before_recipe_id, "before_recipe_id")
        require_uuid(self.after_recipe_id, "after_recipe_id")
        if self.before_recipe_id == self.after_recipe_id:
            raise MigrationRunPlanningError("A Recipe cannot depend on itself")
        object.__setattr__(
            self,
            "kind",
            required_text(self.kind, "dependency kind", maximum=80),
        )
        object.__setattr__(
            self,
            "reason",
            required_text(self.reason, "dependency reason", maximum=1_000),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "after_recipe_id": self.after_recipe_id,
            "before_recipe_id": self.before_recipe_id,
            "kind": self.kind,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True, order=True)
class OdooModelRequirement:
    """Hold the unioned fields required from one Odoo model."""

    model: str
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "model",
            required_text(self.model, "Odoo model", maximum=200),
        )
        fields = tuple(
            sorted(required_text(item, "Odoo field", maximum=200) for item in self.fields)
        )
        if not fields or len(set(fields)) != len(fields):
            raise MigrationRunPlanningError(
                "An Odoo model requirement needs distinct fields"
            )
        object.__setattr__(self, "fields", fields)

    def to_dict(self) -> dict[str, object]:
        return {"fields": list(self.fields), "model": self.model}


@dataclass(frozen=True, slots=True, order=True)
class ReferenceRequirement:
    """Pin one reusable reference dataset required by a Recipe revision."""

    name: str
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            required_text(self.name, "reference name", maximum=160),
        )
        require_hash(self.content_hash, "reference content_hash")

    def to_dict(self) -> dict[str, str]:
        return {"content_hash": self.content_hash, "name": self.name}


@dataclass(frozen=True, slots=True)
class RunTargetBinding:
    """Bind one MigrationRun to one non-secret Odoo target generation."""

    target_binding_id: str
    project_id: str
    migration_run_id: str
    environment: str
    connection_target_hash: str
    credential_role: str
    credential_generation: str
    principal_hash: str
    permission_hash: str
    context_hash: str
    schema_dependency_hash: str
    reference_snapshot_hashes: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.target_binding_id, "target_binding_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
        ):
            require_uuid(value, name)
        if self.environment not in {"TEST", "PRODUCTION"}:
            raise MigrationRunPlanningError("Target environment is invalid")
        if self.credential_role not in {"READ", "WRITE"}:
            raise MigrationRunPlanningError("Target credential role is invalid")
        object.__setattr__(
            self,
            "credential_generation",
            required_text(
                self.credential_generation,
                "credential_generation",
                maximum=300,
            ),
        )
        for value, name in (
            (self.connection_target_hash, "connection_target_hash"),
            (self.principal_hash, "principal_hash"),
            (self.permission_hash, "permission_hash"),
            (self.context_hash, "context_hash"),
            (self.schema_dependency_hash, "schema_dependency_hash"),
        ):
            require_hash(value, name)
        references = tuple(sorted(self.reference_snapshot_hashes))
        for value in references:
            require_hash(value, "reference_snapshot_hash")
        if len(set(references)) != len(references):
            raise MigrationRunPlanningError("Reference snapshots are duplicated")
        object.__setattr__(self, "reference_snapshot_hashes", references)
        require_aware(self.created_at, "created_at")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "connection_target_hash": self.connection_target_hash,
            "context_hash": self.context_hash,
            "created_at": self.created_at.isoformat(),
            "credential_generation": self.credential_generation,
            "credential_role": self.credential_role,
            "environment": self.environment,
            "migration_run_id": self.migration_run_id,
            "permission_hash": self.permission_hash,
            "principal_hash": self.principal_hash,
            "project_id": self.project_id,
            "reference_snapshot_hashes": list(self.reference_snapshot_hashes),
            "schema_dependency_hash": self.schema_dependency_hash,
            "target_binding_id": self.target_binding_id,
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass(frozen=True, slots=True)
class MigrationRunRequirementPlan:
    """Persist one validated union of Recipe and target requirements."""

    migration_run_id: str
    project_id: str
    data_version_id: str
    target_binding_id: str
    selected_revisions: tuple[RecipeRevisionSelection, ...]
    dependencies: tuple[RecipeDependency, ...]
    model_requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    application_order: tuple[str, ...]
    created_at: datetime
    contract_version: int = MIGRATION_RUN_REQUIREMENT_PLAN_VERSION

    def __post_init__(self) -> None:
        for value, name in (
            (self.migration_run_id, "migration_run_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
            (self.target_binding_id, "target_binding_id"),
        ):
            require_uuid(value, name)
        if self.contract_version != MIGRATION_RUN_REQUIREMENT_PLAN_VERSION:
            raise MigrationRunPlanningError("Run requirement plan is unsupported")
        selections = tuple(sorted(self.selected_revisions))
        if not selections:
            raise MigrationRunPlanningError("Select at least one Recipe revision")
        recipe_ids = tuple(item.recipe_id for item in selections)
        if len(set(recipe_ids)) != len(recipe_ids):
            raise MigrationRunPlanningError("Select each Recipe only once")
        object.__setattr__(self, "selected_revisions", selections)
        dependencies = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (item.before_recipe_id, item.after_recipe_id),
            )
        )
        if len({(item.before_recipe_id, item.after_recipe_id) for item in dependencies}) != len(
            dependencies
        ):
            raise MigrationRunPlanningError("Dependency edges are duplicated")
        if any(
            item.before_recipe_id not in recipe_ids
            or item.after_recipe_id not in recipe_ids
            for item in dependencies
        ):
            raise MigrationRunPlanningError(
                "Every dependency must connect selected Recipes"
            )
        object.__setattr__(self, "dependencies", dependencies)
        requirements = tuple(sorted(self.model_requirements))
        if len({item.model for item in requirements}) != len(requirements):
            raise MigrationRunPlanningError("Odoo model requirements are duplicated")
        object.__setattr__(self, "model_requirements", requirements)
        references = tuple(sorted(self.reference_requirements))
        if len({item.name for item in references}) != len(references):
            raise MigrationRunPlanningError(
                "Reference dataset requirements are duplicated"
            )
        object.__setattr__(self, "reference_requirements", references)
        if set(self.application_order) != set(recipe_ids) or len(
            self.application_order
        ) != len(recipe_ids):
            raise MigrationRunPlanningError("Application order is incomplete")
        for recipe_id in self.application_order:
            require_uuid(recipe_id, "application order Recipe")
        position = {
            recipe_id: index for index, recipe_id in enumerate(self.application_order)
        }
        if any(
            position[item.before_recipe_id] >= position[item.after_recipe_id]
            for item in dependencies
        ):
            raise MigrationRunPlanningError("Application order violates a dependency")
        require_aware(self.created_at, "created_at")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "application_order": list(self.application_order),
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "data_version_id": self.data_version_id,
            "dependencies": [item.to_dict() for item in self.dependencies],
            "migration_run_id": self.migration_run_id,
            "model_requirements": [
                item.to_dict() for item in self.model_requirements
            ],
            "project_id": self.project_id,
            "reference_requirements": [
                item.to_dict() for item in self.reference_requirements
            ],
            "selected_revisions": [
                item.to_dict() for item in self.selected_revisions
            ],
            "target_binding_id": self.target_binding_id,
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass(frozen=True, slots=True)
class RunRecipeApplication:
    """Bind one Recipe revision to one isolated workspace inside a run."""

    application_id: str
    project_id: str
    migration_run_id: str
    data_version_id: str
    workspace_id: str
    recipe_id: str
    recipe_revision: int
    recipe_semantic_hash: str
    target_binding_id: str
    physical_binding_hash: str
    parameter_values_hash: str
    status: RecipeApplicationStatus
    issue_hash: str
    mapping_id: str | None
    mapping_content_hash: str | None
    evidence_hash: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.application_id, "application_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
            (self.data_version_id, "data_version_id"),
            (self.workspace_id, "workspace_id"),
            (self.recipe_id, "recipe_id"),
            (self.target_binding_id, "target_binding_id"),
        ):
            require_uuid(value, name)
        require_revision(self.recipe_revision, "recipe_revision")
        for value, name in (
            (self.recipe_semantic_hash, "recipe_semantic_hash"),
            (self.physical_binding_hash, "physical_binding_hash"),
            (self.parameter_values_hash, "parameter_values_hash"),
            (self.issue_hash, "issue_hash"),
            (self.evidence_hash, "evidence_hash"),
        ):
            require_hash(value, name)
        if self.mapping_id is not None:
            require_uuid(self.mapping_id, "mapping_id")
        if self.mapping_content_hash is not None:
            require_hash(self.mapping_content_hash, "mapping_content_hash")
        if (self.mapping_id is None) != (self.mapping_content_hash is None):
            raise MigrationRunPlanningError(
                "Mapping identity and content hash must be stored together"
            )
        object.__setattr__(self, "status", RecipeApplicationStatus(self.status))
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class PlannedRecipeApplication:
    """Carry one application and its bounded provisioning projections."""

    application: RunRecipeApplication
    workspace: MigrationWorkspace
    dataset_ids: tuple[str, ...]
    requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    issues: tuple[MigrationRunPlanIssue, ...]

    def __post_init__(self) -> None:
        if self.application.workspace_id != self.workspace.workspace_id:
            raise MigrationRunPlanningError(
                "RecipeApplication and workspace identities do not match"
            )
        dataset_ids = tuple(sorted(self.dataset_ids))
        if len(set(dataset_ids)) != len(dataset_ids):
            raise MigrationRunPlanningError(
                "Application dataset identities are duplicated"
            )
        object.__setattr__(self, "dataset_ids", dataset_ids)
        object.__setattr__(self, "requirements", tuple(sorted(self.requirements)))
        object.__setattr__(
            self,
            "reference_requirements",
            tuple(sorted(self.reference_requirements)),
        )
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(self.issues, key=lambda item: (item.level, item.code))),
        )


@dataclass(frozen=True, slots=True)
class IntegratedRunProgress:
    """Render one run status without opening its application workspaces."""

    migration_run_id: str
    total_applications: int
    status_counts: Mapping[RecipeApplicationStatus, int]
    next_application_id: str | None

    def __post_init__(self) -> None:
        require_uuid(self.migration_run_id, "migration_run_id")
        if self.total_applications < 0 or sum(self.status_counts.values()) != self.total_applications:
            raise MigrationRunPlanningError("Integrated progress counts are invalid")
        if self.next_application_id is not None:
            require_uuid(self.next_application_id, "next_application_id")


@dataclass(frozen=True, slots=True)
class IntegratedRunBundle:
    """Return one run and its bounded M4 application identities."""

    run: MigrationRun
    target_binding: RunTargetBinding
    requirement_plan: MigrationRunRequirementPlan
    applications: tuple[RunRecipeApplication, ...]
    workspaces: tuple[MigrationWorkspace, ...]
