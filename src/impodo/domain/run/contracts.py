"""Define run-level planning for several Project-owned Recipe revisions.

Integrated Test planning binds one exact Test DataVersion and Odoo target to a
MigrationRun.  The run owns the unioned target requirements and dependency
order.  Each selected Recipe revision receives a separate application and
MigrationWorkspace; no mutable workspace state is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
from typing import Mapping

from impodo.domain.coverage import ReferenceBundle, ReferenceDataSet
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from impodo.domain.run.models import MigrationRun
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaModel


MIGRATION_RUN_REQUIREMENT_PLAN_VERSION = 1
MIGRATION_RUN_EVIDENCE_CONTRACT_VERSION = 1


class MigrationRunPlanningError(MigrationFoundationError):
    """Reject an unsafe integrated run before application provisioning."""


@dataclass(frozen=True, slots=True)
class MigrationRunReferenceBundle:
    """Freeze run-owned reference inputs without borrowing a workspace identity."""

    migration_run_id: str
    source_workspace_id: str
    source_bundle_hash: str
    datasets: tuple[ReferenceDataSet, ...]
    contract_version: int = MIGRATION_RUN_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.migration_run_id, "migration_run_id")
        require_uuid(self.source_workspace_id, "source_workspace_id")
        require_hash(self.source_bundle_hash, "source_bundle_hash")
        if self.contract_version != MIGRATION_RUN_EVIDENCE_CONTRACT_VERSION:
            raise MigrationRunPlanningError(
                "MigrationRun reference evidence contract is unsupported"
            )
        expected = tuple(
            sorted(self.datasets, key=lambda item: (item.reference_id, item.version))
        )
        if self.datasets != expected:
            raise MigrationRunPlanningError(
                "MigrationRun reference datasets are not in canonical order"
            )
        reference_ids = [item.reference_id for item in self.datasets]
        if len(set(reference_ids)) != len(reference_ids):
            raise MigrationRunPlanningError(
                "MigrationRun reference evidence contains duplicate datasets"
            )

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": self.contract_version,
            "datasets": [item.to_portable_dict() for item in self.datasets],
            "migration_run_id": self.migration_run_id,
            "source_bundle_hash": self.source_bundle_hash,
            "source_workspace_id": self.source_workspace_id,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def capture(
        cls,
        migration_run_id: str,
        source: ReferenceBundle,
        datasets: tuple[ReferenceDataSet, ...],
    ) -> "MigrationRunReferenceBundle":
        available = {
            (item.reference_id, item.version, item.content_hash)
            for item in source.datasets
        }
        if any(
            (item.reference_id, item.version, item.content_hash) not in available
            for item in datasets
        ):
            raise MigrationRunPlanningError(
                "MigrationRun reference evidence is not part of its source bundle"
            )
        return cls(
            migration_run_id=migration_run_id,
            source_workspace_id=source.workspace_id,
            source_bundle_hash=source.content_hash,
            datasets=tuple(
                sorted(datasets, key=lambda item: (item.reference_id, item.version))
            ),
        )

    def for_workspace(self, workspace_id: str) -> ReferenceBundle:
        require_uuid(workspace_id, "workspace_id")
        return ReferenceBundle(workspace_id=workspace_id, datasets=self.datasets)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "MigrationRunReferenceBundle":
        expected_keys = {
            "content_hash",
            "contract_version",
            "datasets",
            "migration_run_id",
            "source_bundle_hash",
            "source_workspace_id",
        }
        if set(payload) != expected_keys:
            raise MigrationRunPlanningError(
                "Stored MigrationRun reference evidence has an invalid shape"
            )
        result = cls(
            contract_version=int(payload["contract_version"]),
            migration_run_id=str(payload["migration_run_id"]),
            source_workspace_id=str(payload["source_workspace_id"]),
            source_bundle_hash=str(payload["source_bundle_hash"]),
            datasets=tuple(
                ReferenceDataSet.from_dict(item)
                for item in payload["datasets"]  # type: ignore[union-attr]
            ),
        )
        if payload["content_hash"] != result.content_hash:
            raise MigrationRunPlanningError(
                "Stored MigrationRun reference evidence hash is invalid"
            )
        return result


@dataclass(frozen=True, slots=True)
class MigrationRunTargetSchema:
    """Freeze a run-owned target schema while retaining its capture provenance."""

    migration_run_id: str
    source_schema: OdooSchemaCatalog
    model_names: tuple[str, ...]
    contract_version: int = MIGRATION_RUN_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.migration_run_id, "migration_run_id")
        if self.contract_version != MIGRATION_RUN_EVIDENCE_CONTRACT_VERSION:
            raise MigrationRunPlanningError(
                "MigrationRun target schema contract is unsupported"
            )
        expected = tuple(sorted(set(self.model_names)))
        if self.model_names != expected:
            raise MigrationRunPlanningError(
                "MigrationRun target models are not in canonical order"
            )
        available = {item.name for item in self.source_schema.models}
        if len(available) != len(self.source_schema.models):
            raise MigrationRunPlanningError(
                "MigrationRun source schema contains duplicate models"
            )
        if not set(self.model_names).issubset(available):
            raise MigrationRunPlanningError(
                "MigrationRun target schema names an unavailable model"
            )

    @property
    def models(self) -> tuple[SchemaModel, ...]:
        available = {item.name: item for item in self.source_schema.models}
        return tuple(available[name] for name in self.model_names)

    @property
    def connection_target_hash(self) -> str:
        return self.source_schema.connection_target_hash

    @property
    def captured_at(self) -> datetime:
        return self.source_schema.captured_at

    @property
    def content_hash(self) -> str:
        return content_hash(
            {
                "contract_version": self.contract_version,
                "migration_run_id": self.migration_run_id,
                "model_names": list(self.model_names),
                "source_schema_hash": self.source_schema.content_hash,
                "source_workspace_id": self.source_schema.workspace_id,
            }
        )

    @classmethod
    def capture(
        cls,
        migration_run_id: str,
        source_schema: OdooSchemaCatalog,
        model_names: set[str],
    ) -> "MigrationRunTargetSchema":
        return cls(
            migration_run_id=migration_run_id,
            source_schema=source_schema,
            model_names=tuple(sorted(model_names)),
        )

    def for_workspace(
        self,
        workspace_id: str,
        *,
        models: tuple[SchemaModel, ...] | None = None,
        projection_hash: str | None = None,
    ) -> OdooSchemaCatalog:
        require_uuid(workspace_id, "workspace_id")
        selected = models if models is not None else self.models
        run_models = {item.name for item in self.models}
        if len({item.name for item in selected}) != len(selected) or any(
            item.name not in run_models for item in selected
        ):
            raise MigrationRunPlanningError(
                "Workspace schema projection exceeds its MigrationRun evidence"
            )
        return replace(
            self.source_schema,
            workspace_id=workspace_id,
            models=selected,
            content_hash=(
                projection_hash
                or content_hash(
                    {
                        "migration_run_schema_hash": self.content_hash,
                        "models": [item.name for item in selected],
                        "workspace_id": workspace_id,
                    }
                )
            ),
        )

    def to_json(self) -> str:
        return canonical_json(
            {
                "content_hash": self.content_hash,
                "contract_version": self.contract_version,
                "migration_run_id": self.migration_run_id,
                "model_names": list(self.model_names),
                "source_schema": json.loads(self.source_schema.to_json()),
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "MigrationRunTargetSchema":
        payload = json.loads(value)
        expected_keys = {
            "content_hash",
            "contract_version",
            "migration_run_id",
            "model_names",
            "source_schema",
        }
        if set(payload) != expected_keys:
            raise MigrationRunPlanningError(
                "Stored MigrationRun target schema has an invalid shape"
            )
        result = cls(
            contract_version=int(payload["contract_version"]),
            migration_run_id=str(payload["migration_run_id"]),
            source_schema=OdooSchemaCatalog.from_json(
                canonical_json(payload["source_schema"])
            ),
            model_names=tuple(str(item) for item in payload["model_names"]),
        )
        if payload["content_hash"] != result.content_hash:
            raise MigrationRunPlanningError(
                "Stored MigrationRun target schema hash is invalid"
            )
        return result


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
    QUALIFIED = "QUALIFIED"
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
    """Return one run and its bounded application identities."""

    run: MigrationRun
    target_binding: RunTargetBinding
    requirement_plan: MigrationRunRequirementPlan
    applications: tuple[RunRecipeApplication, ...]
    workspaces: tuple[MigrationWorkspace, ...]
