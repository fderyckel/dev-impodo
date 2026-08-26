"""Define immutable Project-level CutoverPlan and qualification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from impodo.domain.shared.access import ActorIdentity
from ...domain.serialization import content_hash
from impodo.domain.project.foundation import (
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from impodo.domain.run.contracts import RecipeDependency, RecipeRevisionSelection


PROJECT_SHARED_CONTROL_IDS = (
    "control:project.integrated_reconciliation",
    "control:project.package_completeness",
)
CUTOVER_PLAN_CONTRACT_VERSION = 1
APPLICATION_QUALIFICATION_CONTRACT_VERSION = 1
INTEGRATED_QUALIFICATION_CONTRACT_VERSION = 1


class MigrationCutoverError(ValueError):
    """Reject an unsafe plan, qualification, or Project selection."""


class CutoverQualificationState(StrEnum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    QUALIFIED = "QUALIFIED"
    SELECTED = "SELECTED"


@dataclass(frozen=True, slots=True, order=True)
class CutoverWriteOwnership:
    """Assign one writable Odoo field to one exact Recipe."""

    recipe_id: str
    model: str
    field: str

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        object.__setattr__(
            self,
            "model",
            required_text(self.model, "Odoo model", maximum=200),
        )
        object.__setattr__(
            self,
            "field",
            required_text(self.field, "Odoo field", maximum=200),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "model": self.model,
            "recipe_id": self.recipe_id,
        }


@dataclass(frozen=True, slots=True)
class CutoverPlan:
    """Own immutable integrated plan revisions for one Project."""

    cutover_plan_id: str
    project_id: str
    display_name: str
    current_revision: int
    optimistic_revision: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        require_uuid(self.cutover_plan_id, "cutover_plan_id")
        require_uuid(self.project_id, "project_id")
        object.__setattr__(
            self,
            "display_name",
            required_text(self.display_name, "display_name", maximum=200),
        )
        require_revision(self.current_revision, "current_revision")
        require_revision(self.optimistic_revision, "optimistic_revision")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.archived_at is not None:
            require_aware(self.archived_at, "archived_at")


@dataclass(frozen=True, slots=True)
class CutoverPlanRevision:
    """Pin one exact integrated Recipe set, order, ownership, and controls."""

    cutover_plan_id: str
    project_id: str
    version: int
    parent_version: int | None
    selected_revisions: tuple[RecipeRevisionSelection, ...]
    dependencies: tuple[RecipeDependency, ...]
    write_ownership: tuple[CutoverWriteOwnership, ...]
    shared_control_ids: tuple[str, ...]
    requirement_plan_hash: str
    created_by: ActorIdentity
    created_at: datetime
    meaning_hash: str
    content_hash: str
    contract_version: int = CUTOVER_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_uuid(self.cutover_plan_id, "cutover_plan_id")
        require_uuid(self.project_id, "project_id")
        require_revision(self.version, "version")
        if self.parent_version is not None:
            require_revision(self.parent_version, "parent_version")
            if self.parent_version >= self.version:
                raise MigrationCutoverError("CutoverPlan parent revision is invalid")
        if self.contract_version != CUTOVER_PLAN_CONTRACT_VERSION:
            raise MigrationCutoverError("CutoverPlan contract version is unsupported")
        selected = tuple(sorted(self.selected_revisions))
        dependencies = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (
                    item.before_recipe_id,
                    item.after_recipe_id,
                    item.kind,
                ),
            )
        )
        ownership = tuple(sorted(self.write_ownership))
        controls = tuple(sorted(self.shared_control_ids))
        if (
            not selected
            or selected != self.selected_revisions
            or dependencies != self.dependencies
            or ownership != self.write_ownership
            or controls != self.shared_control_ids
            or len(set(controls)) != len(controls)
        ):
            raise MigrationCutoverError("CutoverPlan collections are not canonical")
        recipe_ids = {item.recipe_id for item in selected}
        if len(recipe_ids) != len(selected):
            raise MigrationCutoverError("CutoverPlan selects one revision per Recipe")
        if any(
            edge.before_recipe_id not in recipe_ids
            or edge.after_recipe_id not in recipe_ids
            for edge in dependencies
        ):
            raise MigrationCutoverError("CutoverPlan dependency is outside the plan")
        claims = {(item.model, item.field) for item in ownership}
        if len(claims) != len(ownership) or any(
            item.recipe_id not in recipe_ids for item in ownership
        ):
            raise MigrationCutoverError("CutoverPlan write ownership overlaps")
        if controls != PROJECT_SHARED_CONTROL_IDS:
            raise MigrationCutoverError("Project shared controls are incomplete")
        require_hash(self.requirement_plan_hash, "requirement_plan_hash")
        require_aware(self.created_at, "created_at")
        require_hash(self.meaning_hash, "meaning_hash")
        require_hash(self.content_hash, "content_hash")
        if self.meaning_hash != content_hash(self.meaning_dict()):
            raise MigrationCutoverError("CutoverPlan meaning hash is inconsistent")
        if self.content_hash != content_hash(self.to_dict(include_hashes=False)):
            raise MigrationCutoverError("CutoverPlan content hash is inconsistent")

    def meaning_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "dependencies": [item.to_dict() for item in self.dependencies],
            "requirement_plan_hash": self.requirement_plan_hash,
            "selected_revisions": [
                item.to_dict() for item in self.selected_revisions
            ],
            "shared_control_ids": list(self.shared_control_ids),
            "write_ownership": [item.to_dict() for item in self.write_ownership],
        }

    def to_dict(self, *, include_hashes: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            **self.meaning_dict(),
            "created_at": self.created_at.isoformat(),
            "created_by": {
                "display_name": self.created_by.display_name,
                "issuer": self.created_by.issuer,
                "subject_id": self.created_by.subject_id,
            },
            "cutover_plan_id": self.cutover_plan_id,
            "parent_version": self.parent_version,
            "project_id": self.project_id,
            "version": self.version,
        }
        if include_hashes:
            payload["content_hash"] = self.content_hash
            payload["meaning_hash"] = self.meaning_hash
        return payload


@dataclass(frozen=True, slots=True)
class RunCutoverPlanBinding:
    migration_run_id: str
    cutover_plan_id: str
    cutover_plan_revision: int
    plan_content_hash: str
    bound_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.migration_run_id, "migration_run_id")
        require_uuid(self.cutover_plan_id, "cutover_plan_id")
        require_revision(self.cutover_plan_revision, "cutover_plan_revision")
        require_hash(self.plan_content_hash, "plan_content_hash")
        require_aware(self.bound_at, "bound_at")


@dataclass(frozen=True, slots=True)
class QualifiedOutcomes:
    create_count: int
    update_count: int
    unchanged_count: int
    verified_count: int

    def __post_init__(self) -> None:
        values = (
            self.create_count,
            self.update_count,
            self.unchanged_count,
            self.verified_count,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ):
            raise MigrationCutoverError("Qualification outcome counts are invalid")
        if self.verified_count != self.total_count:
            raise MigrationCutoverError(
                "Verified outcomes must equal new, changed, and unchanged records"
            )

    @property
    def total_count(self) -> int:
        return self.create_count + self.update_count + self.unchanged_count

    def to_dict(self) -> dict[str, int]:
        return {
            "create_count": self.create_count,
            "unchanged_count": self.unchanged_count,
            "update_count": self.update_count,
            "verified_count": self.verified_count,
        }


@dataclass(frozen=True, slots=True)
class ApplicationQualificationEvidence:
    """Bind one Recipe application to complete Test execution evidence."""

    application_id: str
    project_id: str
    migration_run_id: str
    workspace_id: str
    recipe_id: str
    recipe_revision: int
    recipe_semantic_hash: str
    target_binding_hash: str
    mapping_content_hash: str
    preparation_hash: str
    quality_hash: str
    comparison_hash: str
    execution_hash: str
    read_back_hash: str
    reconciliation_hash: str
    control_hash: str
    outcomes: QualifiedOutcomes
    execution_started_at: datetime
    execution_completed_at: datetime
    reconciled_at: datetime
    content_hash: str
    contract_version: int = APPLICATION_QUALIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for value, label in (
            (self.application_id, "application_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
            (self.workspace_id, "workspace_id"),
            (self.recipe_id, "recipe_id"),
        ):
            require_uuid(value, label)
        require_revision(self.recipe_revision, "recipe_revision")
        for value, label in (
            (self.recipe_semantic_hash, "recipe_semantic_hash"),
            (self.target_binding_hash, "target_binding_hash"),
            (self.mapping_content_hash, "mapping_content_hash"),
            (self.preparation_hash, "preparation_hash"),
            (self.quality_hash, "quality_hash"),
            (self.comparison_hash, "comparison_hash"),
            (self.execution_hash, "execution_hash"),
            (self.read_back_hash, "read_back_hash"),
            (self.reconciliation_hash, "reconciliation_hash"),
            (self.control_hash, "control_hash"),
            (self.content_hash, "content_hash"),
        ):
            require_hash(value, label)
        require_aware(self.execution_started_at, "execution_started_at")
        require_aware(self.execution_completed_at, "execution_completed_at")
        require_aware(self.reconciled_at, "reconciled_at")
        if not (
            self.execution_started_at
            <= self.execution_completed_at
            <= self.reconciled_at
        ):
            raise MigrationCutoverError("Application evidence time order is invalid")
        if self.contract_version != APPLICATION_QUALIFICATION_CONTRACT_VERSION:
            raise MigrationCutoverError(
                "Application qualification contract version is unsupported"
            )
        if self.content_hash != content_hash(self.to_dict(include_hash=False)):
            raise MigrationCutoverError(
                "Application qualification evidence hash is inconsistent"
            )

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "application_id": self.application_id,
            "comparison_hash": self.comparison_hash,
            "contract_version": self.contract_version,
            "control_hash": self.control_hash,
            "execution_completed_at": self.execution_completed_at.isoformat(),
            "execution_hash": self.execution_hash,
            "execution_started_at": self.execution_started_at.isoformat(),
            "mapping_content_hash": self.mapping_content_hash,
            "migration_run_id": self.migration_run_id,
            "outcomes": self.outcomes.to_dict(),
            "preparation_hash": self.preparation_hash,
            "project_id": self.project_id,
            "quality_hash": self.quality_hash,
            "read_back_hash": self.read_back_hash,
            "recipe_id": self.recipe_id,
            "recipe_revision": self.recipe_revision,
            "recipe_semantic_hash": self.recipe_semantic_hash,
            "reconciled_at": self.reconciled_at.isoformat(),
            "reconciliation_hash": self.reconciliation_hash,
            "target_binding_hash": self.target_binding_hash,
            "workspace_id": self.workspace_id,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload


@dataclass(frozen=True, slots=True)
class RecipeApplicationQualification:
    qualification_id: str
    project_id: str
    recipe_id: str
    recipe_revision: int
    application_id: str
    test_target_binding_hash: str
    outcomes: QualifiedOutcomes
    evidence_hash: str
    evidence_storage_key: str
    artifact_hash: str
    qualified_by: ActorIdentity
    qualified_at: datetime
    status: str = "TEST_QUALIFIED"

    def __post_init__(self) -> None:
        for value, label in (
            (self.qualification_id, "qualification_id"),
            (self.project_id, "project_id"),
            (self.recipe_id, "recipe_id"),
            (self.application_id, "application_id"),
        ):
            require_uuid(value, label)
        require_revision(self.recipe_revision, "recipe_revision")
        require_hash(self.test_target_binding_hash, "test_target_binding_hash")
        require_hash(self.evidence_hash, "evidence_hash")
        require_hash(self.artifact_hash, "artifact_hash")
        required_text(self.evidence_storage_key, "evidence_storage_key", maximum=800)
        require_aware(self.qualified_at, "qualified_at")
        if self.status != "TEST_QUALIFIED":
            raise MigrationCutoverError("Recipe application qualification is invalid")


@dataclass(frozen=True, slots=True)
class CutoverPlanQualification:
    qualification_id: str
    project_id: str
    cutover_plan_id: str
    cutover_plan_revision: int
    plan_content_hash: str
    test_run_id: str
    application_ids: tuple[str, ...]
    application_qualification_ids: tuple[str, ...]
    target_binding_hash: str
    requirement_plan_hash: str
    integrated_evidence_hash: str
    evidence_storage_key: str
    artifact_hash: str
    qualified_by: ActorIdentity
    qualified_at: datetime
    status: str = "TEST_QUALIFIED"

    def __post_init__(self) -> None:
        for value, label in (
            (self.qualification_id, "qualification_id"),
            (self.project_id, "project_id"),
            (self.cutover_plan_id, "cutover_plan_id"),
            (self.test_run_id, "test_run_id"),
        ):
            require_uuid(value, label)
        require_revision(self.cutover_plan_revision, "cutover_plan_revision")
        applications = tuple(sorted(self.application_ids))
        qualifications = tuple(sorted(self.application_qualification_ids))
        if (
            not applications
            or applications != self.application_ids
            or qualifications != self.application_qualification_ids
            or len(applications) != len(qualifications)
        ):
            raise MigrationCutoverError("Integrated qualification members are invalid")
        for value in (*applications, *qualifications):
            require_uuid(value, "qualification member")
        for value, label in (
            (self.plan_content_hash, "plan_content_hash"),
            (self.target_binding_hash, "target_binding_hash"),
            (self.requirement_plan_hash, "requirement_plan_hash"),
            (self.integrated_evidence_hash, "integrated_evidence_hash"),
            (self.artifact_hash, "artifact_hash"),
        ):
            require_hash(value, label)
        required_text(self.evidence_storage_key, "evidence_storage_key", maximum=800)
        require_aware(self.qualified_at, "qualified_at")
        if self.status != "TEST_QUALIFIED":
            raise MigrationCutoverError("Integrated qualification is invalid")


@dataclass(frozen=True, slots=True)
class ProjectCutoverSelection:
    cutover_selection_id: str
    project_id: str
    cutover_plan_id: str
    cutover_plan_revision: int
    qualification_id: str
    content_hash: str
    selected_by: ActorIdentity
    selected_at: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.cutover_selection_id, "cutover_selection_id"),
            (self.project_id, "project_id"),
            (self.cutover_plan_id, "cutover_plan_id"),
            (self.qualification_id, "qualification_id"),
        ):
            require_uuid(value, label)
        require_revision(self.cutover_plan_revision, "cutover_plan_revision")
        require_hash(self.content_hash, "content_hash")
        require_aware(self.selected_at, "selected_at")
        expected = content_hash(
            {
                "cutover_plan_id": self.cutover_plan_id,
                "cutover_plan_revision": self.cutover_plan_revision,
                "qualification_id": self.qualification_id,
                "project_id": self.project_id,
            }
        )
        if self.content_hash != expected:
            raise MigrationCutoverError("Project cutover selection hash is invalid")


@dataclass(frozen=True, slots=True, order=True)
class CutoverQualificationIssue:
    code: str
    message: str
    recovery_action: str
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", required_text(self.code, "code", maximum=120))
        object.__setattr__(
            self,
            "message",
            required_text(self.message, "message", maximum=2_000),
        )
        object.__setattr__(
            self,
            "recovery_action",
            required_text(
                self.recovery_action,
                "recovery_action",
                maximum=2_000,
            ),
        )
        if self.workspace_id is not None:
            require_uuid(self.workspace_id, "workspace_id")


def integrated_evidence_payload(
    *,
    plan: CutoverPlanRevision,
    run_id: str,
    target_binding_hash: str,
    applications: tuple[ApplicationQualificationEvidence, ...],
    shared_controls: Mapping[str, bool],
) -> dict[str, object]:
    """Build the canonical exact evidence qualified by one Project action."""

    return {
        "applications": [item.to_dict() for item in applications],
        "contract_version": INTEGRATED_QUALIFICATION_CONTRACT_VERSION,
        "cutover_plan_content_hash": plan.content_hash,
        "cutover_plan_id": plan.cutover_plan_id,
        "cutover_plan_revision": plan.version,
        "migration_run_id": require_uuid(run_id, "migration_run_id"),
        "project_id": plan.project_id,
        "shared_controls": {
            key: bool(shared_controls[key]) for key in sorted(shared_controls)
        },
        "target_binding_hash": require_hash(
            target_binding_hash,
            "target_binding_hash",
        ),
    }
