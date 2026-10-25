"""Persistence boundaries owned by the run application services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from impodo.domain.coverage import ReferenceBundle
from impodo.domain.cutover.models import (
    ApplicationQualificationEvidence,
    CutoverPlan,
    CutoverPlanQualification,
    CutoverPlanRevision,
    CutoverWriteOwnership,
    ProjectCutoverSelection,
    RunCutoverPlanBinding,
)
from impodo.domain.project.foundation import FaultInjector, MigrationOperationIntent
from impodo.domain.run.contracts import (
    IntegratedRunBundle,
    IntegratedRunProgress,
    MigrationRunPlanIssue,
    MigrationRunReferenceBundle,
    MigrationRunRequirementPlan,
    MigrationRunTargetSchema,
    PlannedRecipeApplication,
    RecipeApplicationStatus,
    RunRecipeApplication,
    RunTargetBinding,
)
from impodo.domain.run.production import ProductionRunBinding
from impodo.domain.run.production_values import ProductionRunValues
from impodo.domain.run.test_setup import TestRunSetupBinding, TestRunValues
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.models import MigrationWorkspace

from ...domain.run.models import MigrationRun


class MigrationRunRepository(Protocol):
    def next_run_number(self, project_id: str) -> int: ...

    def create_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> MigrationRun: ...

    def get_migration_run(self, migration_run_id: str) -> MigrationRun: ...

    def get_operation_intent(
        self,
        operation_id: str,
    ) -> MigrationOperationIntent: ...

    def resume_migration_run_creation(
        self,
        operation_id: str,
        *,
        actor: Actor,
    ) -> MigrationRun: ...

    def list_migration_runs(self, project_id: str) -> tuple[MigrationRun, ...]: ...

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun: ...


class RunRepositoryFoundation(Protocol):
    """Expose registry operations shared by the run-specific repositories."""

    def next_run_number(self, project_id: str) -> int: ...

    def get_migration_run(self, migration_run_id: str) -> MigrationRun: ...

    def save_migration_run(
        self,
        run: MigrationRun,
        *,
        expected_revision: int,
        event_type: str,
        actor: Actor,
    ) -> MigrationRun: ...

    def get_migration_workspace(self, workspace_id: str) -> MigrationWorkspace: ...

    def get_operation_intent(self, operation_id: str) -> MigrationOperationIntent: ...


class RunPlanningRepository(Protocol):
    """Persist integrated run plans, activation intents, and application evidence."""

    @property
    def foundation(self) -> RunRepositoryFoundation: ...

    def provision_integrated_run(
        self,
        *,
        run: MigrationRun,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: MigrationRunTargetSchema,
        reference_bundle: MigrationRunReferenceBundle | None,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle: ...

    def activate_test_run(
        self,
        *,
        run: MigrationRun,
        test_binding: TestRunSetupBinding,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: MigrationRunTargetSchema,
        reference_bundle: MigrationRunReferenceBundle | None,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle: ...

    def resume_test_activation(
        self,
        operation_id: str,
        *,
        migration_run_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle: ...

    def test_activation_operation(
        self,
        migration_run_id: str,
    ) -> MigrationOperationIntent: ...

    def activate_production_run(
        self,
        *,
        run: MigrationRun,
        production_binding: ProductionRunBinding,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: MigrationRunTargetSchema,
        reference_bundle: MigrationRunReferenceBundle | None,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        activation_inputs: Mapping[str, object] | None = None,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle: ...

    def completed_production_activations(self, project_id: str) -> frozenset[str]: ...

    def production_activation_operation(
        self,
        migration_run_id: str,
    ) -> MigrationOperationIntent | None: ...

    def resume_production_activation(
        self,
        operation_id: str,
        *,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle: ...

    def commit_provisioning(self, operation_id: str) -> IntegratedRunBundle: ...

    def get_target_binding(self, migration_run_id: str) -> RunTargetBinding: ...

    def get_run_target_schema(
        self, migration_run_id: str
    ) -> MigrationRunTargetSchema: ...

    def get_run_reference_bundle(
        self,
        migration_run_id: str,
    ) -> MigrationRunReferenceBundle | None: ...

    def get_workspace_reference_bundle(
        self,
        workspace_id: str,
    ) -> ReferenceBundle | None: ...

    def get_workspace_target_schema(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None: ...

    def list_applications(
        self,
        migration_run_id: str,
    ) -> tuple[RunRecipeApplication, ...]: ...

    def list_issues(
        self,
        application_id: str,
    ) -> tuple[MigrationRunPlanIssue, ...]: ...

    def count_blocking_run_issues(
        self,
        migration_run_id: str,
        *,
        code: str,
    ) -> int: ...

    def list_run_issues(
        self,
        migration_run_id: str,
    ) -> Mapping[str, tuple[MigrationRunPlanIssue, ...]]: ...

    def get_application(self, application_id: str) -> RunRecipeApplication: ...

    def save_application_materialization(
        self,
        application_id: str,
        *,
        expected_evidence_hash: str,
        status: RecipeApplicationStatus,
        issues: tuple[MigrationRunPlanIssue, ...],
        mapping_id: str | None,
        mapping_content_hash: str | None,
        evidence_hash: str,
        actor: Actor,
    ) -> RunRecipeApplication: ...

    def transition_application_status(
        self,
        application_id: str,
        *,
        expected_statuses: tuple[RecipeApplicationStatus, ...],
        status: RecipeApplicationStatus,
        actor: Actor,
        expected_mapping_content_hash: str | None = None,
    ) -> RunRecipeApplication: ...

    def progress(self, migration_run_id: str) -> IntegratedRunProgress: ...

    def get_bundle(self, migration_run_id: str) -> IntegratedRunBundle: ...


class TestRunRepository(Protocol):
    """Persist Test setup bindings and their fresh-data values."""

    @property
    def foundation(self) -> RunRepositoryFoundation: ...

    def bind_setup(
        self,
        binding: TestRunSetupBinding,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
    ) -> TestRunSetupBinding: ...

    def get(self, migration_run_id: str) -> TestRunSetupBinding: ...

    def for_workspace(self, workspace_id: str) -> TestRunSetupBinding | None: ...

    def list_for_project(
        self,
        project_id: str,
    ) -> tuple[TestRunSetupBinding, ...]: ...

    def get_run_values(self, migration_run_id: str) -> TestRunValues | None: ...

    def replace_run_values(
        self,
        values: TestRunValues,
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunValues: ...


class ProductionRunRepository(Protocol):
    """Persist Production setup bindings and their fresh-data values."""

    @property
    def foundation(self) -> RunRepositoryFoundation: ...

    def bind_setup(
        self,
        binding: ProductionRunBinding,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> ProductionRunBinding: ...

    def get(self, migration_run_id: str) -> ProductionRunBinding: ...

    def get_run_values(self, migration_run_id: str) -> ProductionRunValues | None: ...

    def replace_run_values(
        self,
        values: ProductionRunValues,
        *,
        expected_revision: int | None,
        expected_binding_hash: str,
        actor: Actor,
    ) -> ProductionRunValues: ...

    def for_workspace(self, workspace_id: str) -> ProductionRunBinding | None: ...

    def list_for_project(
        self,
        project_id: str,
    ) -> tuple[ProductionRunBinding, ...]: ...


class RunCutoverPlanRepository(Protocol):
    """Persist the Cutover-plan evidence needed by Test and Production runs."""

    def ensure_for_run(
        self,
        *,
        project_id: str,
        migration_run_id: str,
        requirement_plan: MigrationRunRequirementPlan,
        write_ownership: tuple[CutoverWriteOwnership, ...],
        operation_id: str,
        actor: Actor,
        display_name: str = "Project cutover plan",
        fault: FaultInjector | None = None,
    ) -> RunCutoverPlanBinding: ...

    def get_plan(self, cutover_plan_id: str) -> CutoverPlan: ...

    def get_revision(
        self,
        cutover_plan_id: str,
        version: int,
    ) -> CutoverPlanRevision: ...

    def get_run_binding(self, migration_run_id: str) -> RunCutoverPlanBinding: ...

    def get_qualification(
        self,
        qualification_id: str,
    ) -> CutoverPlanQualification: ...

    def assert_qualification_authentic(
        self,
        qualification: CutoverPlanQualification,
    ) -> None: ...

    def get_selection(self, selection_id: str) -> ProjectCutoverSelection: ...

    def current_selection(self, project_id: str) -> ProjectCutoverSelection | None: ...

    def list_qualifications(
        self,
        cutover_plan_id: str,
        version: int | None = None,
    ) -> tuple[CutoverPlanQualification, ...]: ...

    def committed_qualification_for_operation(
        self,
        operation_id: str,
        *,
        project_id: str,
        migration_run_id: str,
        integrated_evidence_hash: str,
        expected_workspace_revision: int,
        actor: Actor,
    ) -> CutoverPlanQualification | None: ...

    def qualify(
        self,
        *,
        plan: CutoverPlanRevision,
        migration_run_id: str,
        application_evidence: tuple[ApplicationQualificationEvidence, ...],
        target_binding_hash: str,
        integrated_payload: Mapping[str, object],
        expected_workspace_revision: int,
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CutoverPlanQualification: ...

    def committed_selection_for_operation(
        self,
        operation_id: str,
        *,
        project_id: str,
        qualification_id: str,
        expected_workspace_revision: int,
        actor: Actor,
    ) -> ProjectCutoverSelection | None: ...

    def select(
        self,
        qualification_id: str,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        actor: Actor,
    ) -> ProjectCutoverSelection: ...
