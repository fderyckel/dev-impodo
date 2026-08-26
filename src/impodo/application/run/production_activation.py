"""Restart-safe activation of reviewed Production runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid5

from impodo.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.cutover.models import CutoverPlanRevision
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.serialization import content_hash
from impodo.migration_foundation import (
    FaultInjector,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from impodo.migration_production import (
    ProductionRunBinding,
    ProductionRunBindingState,
    ProductionRunError,
    activation_evidence_hash,
)
from impodo.migration_run_planning import (
    IntegratedRunBundle,
    MigrationRunReferenceBundle,
    MigrationRunRequirementPlan,
    MigrationRunTargetSchema,
    RunTargetBinding,
)
from impodo.models import OdooWriteIdentity
from impodo.workspace_contracts import OdooSchemaCatalog

from .application_materialization import RunApplicationMaterializer
from .production_review import ProductionRunReviewUseCase


class ProductionRunActivationUseCase:
    """Own Production authority checks, reservation, and publication."""

    def __init__(
        self,
        *,
        repository,
        review: ProductionRunReviewUseCase,
        materializer: RunApplicationMaterializer,
        workspace_states,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._review = review
        self._materializer = materializer
        self._workspace_states = workspace_states
        self._authorization = authorization

    def activate(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        production_binding: ProductionRunBinding,
        plan: CutoverPlanRevision,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        test_connection_target_hash: str,
        read_credential_generation: str,
        write_identity: OdooWriteIdentity,
        write_credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Activate one setup run through the existing application compiler."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        read_credential_generation = required_text(
            read_credential_generation,
            "read_credential_generation",
            maximum=300,
        )
        write_credential_generation = required_text(
            write_credential_generation,
            "write_credential_generation",
            maximum=300,
        )
        if read_credential_generation != target_schema.read_credential_binding_hash:
            raise ProductionRunError(
                "The Production schema belongs to another read credential generation"
            )
        if read_credential_generation == write_credential_generation:
            raise ProductionRunError(
                "Production read and write credentials must remain separate"
            )
        if (
            write_identity.target_hash != target_schema.connection_target_hash
            or write_identity.context_hash != target_schema.read_context_hash
        ):
            raise ProductionRunError(
                "The Production write credential does not match the reviewed target context"
            )
        required_write_models = {item.model for item in plan.write_ownership}
        if not required_write_models.issubset(set(write_identity.writable_models)):
            raise ProductionRunError(
                "The Production write credential cannot update every planned Odoo model"
            )
        self._authorization.require(
            actor,
            Capability.PRODUCTION_RUN_ACTIVATE,
            project_id=project_id,
        )
        review = self._review.review(
            project_id,
            production_binding=production_binding,
            plan=plan,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            test_connection_target_hash=test_connection_target_hash,
            parameter_values=parameter_values,
            control_values=control_values,
            shared_control_values=shared_control_values,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise ProductionRunError(f"{first.message} {first.recovery_action}")
        if production_binding.state is ProductionRunBindingState.ACTIVE:
            self._assert_retry_matches(
                production_binding,
                target_schema=target_schema,
                read_credential_generation=read_credential_generation,
                write_identity=write_identity,
                write_credential_generation=write_credential_generation,
                parameter_values=parameter_values,
                control_values=control_values,
                shared_control_values=shared_control_values,
            )
            resumed = self._repository.resume_production_activation(
                operation_id,
                actor=actor,
                fault=fault,
            )
            return self._materializer.materialize(
                resumed,
                review=review,
                operation_id=operation_id,
                ready_event_type="PRODUCTION_RUN_READY",
                target_workspace_state=self._workspace_states.repository.get(
                    production_binding.setup_workspace_id
                ),
                actor=actor,
            )
        if production_binding.state is not ProductionRunBindingState.SETUP:
            raise ProductionRunError("Production run activation is inconsistent")
        current_run = self._repository.foundation.get_migration_run(
            production_binding.migration_run_id
        )
        if (
            current_run.project_id != project_id
            or current_run.data_version_id != production_binding.data_version_id
            or current_run.purpose is not MigrationRunPurpose.PRODUCTION
            or current_run.cutover_selection_id
            != production_binding.cutover_selection_id
            or current_run.target_binding_id is not None
        ):
            raise ProductionRunError(
                "Production run changed before activation; reload its setup"
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
            environment="PRODUCTION",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=read_credential_generation,
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
        parameter_hash = content_hash(parameter_values or {})
        control_hash = content_hash(
            {
                "recipe_controls": control_values or {},
                "shared_controls": dict(shared_control_values),
            }
        )
        write_identity_values = {
            "context_hash": write_identity.context_hash,
            "observed_at": write_identity.observed_at,
            "permission_hash": write_identity.permission_hash,
            "principal_hash": write_identity.principal_hash,
            "readable_models": sorted(set(write_identity.readable_models)),
            "target_hash": write_identity.target_hash,
            "writable_models": sorted(set(write_identity.writable_models)),
        }
        evidence_hash = activation_evidence_hash(
            binding=production_binding,
            target_binding_hash=target.content_hash,
            requirement_plan_hash=requirement_plan.content_hash,
            write_identity=write_identity_values,
            parameter_values_hash=parameter_hash,
            control_values_hash=control_hash,
        )
        active_binding = replace(
            production_binding,
            state=ProductionRunBindingState.ACTIVE,
            target_binding_id=target_binding_id,
            read_credential_generation=read_credential_generation,
            write_credential_generation=write_credential_generation,
            write_principal_hash=write_identity.principal_hash,
            write_permission_hash=write_identity.permission_hash,
            write_context_hash=write_identity.context_hash,
            parameter_values_hash=parameter_hash,
            control_values_hash=control_hash,
            activation_evidence_hash=evidence_hash,
            activated_at=now,
        )
        request_hash = content_hash(
            {
                "control_values": control_values or {},
                "cutover_selection_id": production_binding.cutover_selection_id,
                "data_version_id": run.data_version_id,
                "parameter_values": parameter_values or {},
                "production_setup_hash": production_binding.content_hash,
                "project_id": project_id,
                "read_credential_generation": read_credential_generation,
                "reference_bundle": (
                    run_reference_bundle.to_portable_dict()
                    if run_reference_bundle is not None
                    else None
                ),
                "shared_control_values": dict(shared_control_values),
                "target_schema_hash": run_target_schema.content_hash,
                "write_credential_generation": write_credential_generation,
                "write_identity": {
                    key: value
                    for key, value in write_identity_values.items()
                    if key != "observed_at"
                },
            }
        )
        bundle = self._repository.activate_production_run(
            run=run,
            production_binding=active_binding,
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
        return self._materializer.materialize(
            bundle,
            review=review,
            operation_id=operation_id,
            ready_event_type="PRODUCTION_RUN_READY",
            target_workspace_state=self._workspace_states.repository.get(
                production_binding.setup_workspace_id
            ),
            actor=actor,
        )

    def _assert_retry_matches(
        self,
        binding: ProductionRunBinding,
        *,
        target_schema: OdooSchemaCatalog,
        read_credential_generation: str,
        write_identity: OdooWriteIdentity,
        write_credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
    ) -> None:
        target = self._repository.get_target_binding(binding.migration_run_id)
        parameter_hash = content_hash(parameter_values or {})
        control_hash = content_hash(
            {
                "recipe_controls": control_values or {},
                "shared_controls": dict(shared_control_values),
            }
        )
        if (
            target.connection_target_hash != target_schema.connection_target_hash
            or target.principal_hash != target_schema.read_principal_hash
            or target.permission_hash != target_schema.read_permission_hash
            or target.context_hash != target_schema.read_context_hash
            or binding.read_credential_generation != read_credential_generation
            or binding.write_credential_generation != write_credential_generation
            or binding.write_principal_hash != write_identity.principal_hash
            or binding.write_permission_hash != write_identity.permission_hash
            or binding.write_context_hash != write_identity.context_hash
            or binding.parameter_values_hash != parameter_hash
            or binding.control_values_hash != control_hash
        ):
            raise ProductionRunError(
                "Production activation was already recorded with different evidence"
            )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))
