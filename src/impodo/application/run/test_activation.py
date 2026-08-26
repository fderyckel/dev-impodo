"""Restart-safe provisioning and activation of integrated Test runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid5

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.cutover.models import CutoverWriteOwnership
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.run.models import (
    MigrationRun,
    MigrationRunPurpose,
    MigrationRunState,
)
from impodo.domain.serialization import content_hash
from impodo.domain.project.foundation import (
    FaultInjector,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from impodo.domain.run.contracts import (
    IntegratedRunBundle,
    MigrationRunPlanningError,
    MigrationRunReferenceBundle,
    MigrationRunRequirementPlan,
    MigrationRunTargetSchema,
    RecipeDependency,
    RunTargetBinding,
)
from impodo.domain.run.test_setup import TestRunSetupBinding, TestRunSetupState
from impodo.domain.workspace.contracts import OdooSchemaCatalog

from .application_materialization import RunApplicationMaterializer
from .review import RunReviewUseCase


class TestRunActivationUseCase:
    """Own Test run reservation, activation, and isolated-workspace publication."""

    def __init__(
        self,
        *,
        repository,
        review: RunReviewUseCase,
        materializer: RunApplicationMaterializer,
        workspace_states,
        cutover_plans,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._review = review
        self._materializer = materializer
        self._workspace_states = workspace_states
        self._cutover_plans = cutover_plans
        self._authorization = authorization

    def start(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        credential_generation: str,
        label: str,
        operation_id: str,
        actor: Actor,
        parameter_values: Mapping[str, Mapping[str, object]] | None = None,
        control_values: Mapping[str, Mapping[str, str]] | None = None,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Provision and materialize one restart-safe multi-Recipe Test run."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        credential_generation = required_text(
            credential_generation,
            "credential_generation",
            maximum=300,
        )
        if credential_generation != target_schema.read_credential_binding_hash:
            raise MigrationRunPlanningError(
                "The reviewed Odoo evidence belongs to another read credential generation"
            )
        self._authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        self._authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            project_id=project_id,
        )
        review = self._review.review(
            project_id,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=DataVersionPurpose.TEST,
            required_target_workspace_id=None,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise MigrationRunPlanningError(f"{first.message} {first.recovery_action}")
        now = utc_now()
        run_id = self._child_operation(operation_id, "migration-run")
        target_binding_id = self._child_operation(operation_id, "target-binding")
        required_reference_names = {item.name for item in review.reference_requirements}
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            MigrationRunReferenceBundle.capture(
                run_id,
                target_reference_bundle,
                captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        required_models = {item.model for item in review.model_requirements}
        run_target_schema = MigrationRunTargetSchema.capture(
            run_id,
            target_schema,
            required_models,
        )
        run = MigrationRun(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            run_number=self._repository.foundation.next_run_number(project_id),
            purpose=MigrationRunPurpose.TEST,
            label=clean_label,
            state=MigrationRunState.DRAFT,
            target_binding_id=target_binding_id,
            cutover_selection_id=None,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run_id,
            environment="TEST",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(item.selection for item in review.applications),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._materializer.plan(item, run=run, target=target, now=now)
            for item in review.applications
        )
        request_hash = content_hash(
            {
                "control_values": control_values or {},
                "data_version_id": data_version_id,
                "dependencies": [item.to_dict() for item in review.dependencies],
                "label": clean_label,
                "parameter_values": parameter_values or {},
                "project_id": project_id,
                "selected_revisions": [
                    item.selection.to_dict() for item in review.applications
                ],
                "reference_bundle": (
                    run_reference_bundle.to_portable_dict()
                    if run_reference_bundle is not None
                    else None
                ),
                "target_schema_hash": run_target_schema.content_hash,
            }
        )
        bundle = self._repository.provision_integrated_run(
            run=run,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )
        committed = self._materializer.materialize(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="INTEGRATED_TEST_RUN_READY",
            target_workspace_state=self._workspace_states.repository.get(
                target_schema.workspace_id
            ),
            actor=actor,
        )
        self._ensure_cutover_plan(
            committed,
            review=review,
            operation_id=operation_id,
            actor=actor,
        )
        return committed

    def activate(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        test_binding: TestRunSetupBinding,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Activate one fresh Test setup and create isolated Recipe work areas."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        credential_generation = required_text(
            credential_generation,
            "credential_generation",
            maximum=300,
        )
        if credential_generation != target_schema.read_credential_binding_hash:
            raise MigrationRunPlanningError(
                "The Test schema belongs to another read credential generation"
            )
        if (
            test_binding.project_id != project_id
            or target_schema.workspace_id != test_binding.setup_workspace_id
        ):
            raise MigrationRunPlanningError(
                "The reviewed Odoo target evidence does not belong to this Test setup"
            )
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in test_binding.selected_revisions
        )
        review = self._review.review(
            project_id,
            data_version_id=test_binding.data_version_id,
            recipe_revisions=selected,
            dependencies=test_binding.dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=None,
            purpose=DataVersionPurpose.TEST,
            required_target_workspace_id=None,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise MigrationRunPlanningError(f"{first.message} {first.recovery_action}")
        if test_binding.state is TestRunSetupState.ACTIVE:
            resumed = self._repository.resume_test_activation(
                operation_id,
                actor=actor,
                fault=fault,
            )
            return self._materializer.materialize(
                resumed,
                review=review,
                operation_id=operation_id,
                ready_event_type="INTEGRATED_TEST_RUN_READY",
                target_workspace_state=self._workspace_states.repository.get(
                    test_binding.setup_workspace_id
                ),
                actor=actor,
            )
        current_run = self._repository.foundation.get_migration_run(
            test_binding.migration_run_id
        )
        if (
            current_run.project_id != project_id
            or current_run.data_version_id != test_binding.data_version_id
            or current_run.purpose is not MigrationRunPurpose.TEST
            or current_run.target_binding_id is not None
        ):
            raise MigrationRunPlanningError(
                "Test run changed before activation; reload its setup"
            )
        now = utc_now()
        target_binding_id = self._child_operation(operation_id, "target-binding")
        run = replace(
            current_run,
            target_binding_id=target_binding_id,
            optimistic_revision=current_run.optimistic_revision + 1,
            updated_at=now,
        )
        required_reference_names = {item.name for item in review.reference_requirements}
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            MigrationRunReferenceBundle.capture(
                run.migration_run_id,
                target_reference_bundle,
                captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        run_target_schema = MigrationRunTargetSchema.capture(
            run.migration_run_id,
            target_schema,
            {item.model for item in review.model_requirements},
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            environment="TEST",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run.migration_run_id,
            project_id=project_id,
            data_version_id=run.data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(item.selection for item in review.applications),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._materializer.plan(item, run=run, target=target, now=now)
            for item in review.applications
        )
        active_binding = replace(
            test_binding,
            state=TestRunSetupState.ACTIVE,
            target_binding_id=target_binding_id,
            activated_at=now,
        )
        bundle = self._repository.activate_test_run(
            run=run,
            test_binding=active_binding,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            request_hash=content_hash(
                {
                    "reference_bundle": (
                        run_reference_bundle.to_portable_dict()
                        if run_reference_bundle is not None
                        else None
                    ),
                    "target_schema_hash": run_target_schema.content_hash,
                    "test_setup_hash": test_binding.content_hash,
                    "parameter_values": parameter_values or {},
                }
            ),
            actor=actor,
            fault=fault,
        )
        committed = self._materializer.materialize(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="INTEGRATED_TEST_RUN_READY",
            target_workspace_state=self._workspace_states.repository.get(
                test_binding.setup_workspace_id
            ),
            actor=actor,
        )
        self._ensure_cutover_plan(
            committed,
            review=review,
            operation_id=operation_id,
            actor=actor,
        )
        return committed

    def _ensure_cutover_plan(
        self,
        committed: IntegratedRunBundle,
        *,
        review,
        operation_id: str,
        actor: Actor,
    ) -> None:
        self._cutover_plans.ensure_for_run(
            project_id=committed.run.project_id,
            migration_run_id=committed.run.migration_run_id,
            requirement_plan=committed.requirement_plan,
            write_ownership=tuple(
                sorted(
                    CutoverWriteOwnership(
                        recipe_id=item.selection.recipe_id,
                        model=model,
                        field=field,
                    )
                    for item in review.applications
                    for model, field in item.write_claims
                )
            ),
            operation_id=self._child_operation(operation_id, "cutover-plan"),
            actor=actor,
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))
