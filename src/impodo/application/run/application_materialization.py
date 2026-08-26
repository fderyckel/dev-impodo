"""Provision isolated workspaces and materialize reviewed Recipe applications."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4, uuid5

from impodo.domain.shared.access import Actor
from impodo.application.data_version.source_packages import DataVersionSourcePackage, SourcePackageOrigin
from impodo.domain.run.models import MigrationRun, MigrationRunState
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.models import MigrationWorkspace, MigrationWorkspaceState
from impodo.domain.project.foundation import utc_now
from impodo.domain.run.contracts import (
    IntegratedRunBundle,
    MigrationRunPlanningError,
    PlannedRecipeApplication,
    RecipeApplicationStatus,
    RunRecipeApplication,
    RunTargetBinding,
)
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStateNotFoundError

from .planning_models import IntegratedRunReview, ReviewedRecipeApplication


class RunApplicationMaterializer:
    """Own isolated application identities, workspaces, and compiler publication."""

    def __init__(
        self,
        *,
        repository,
        source_packages,
        projects,
        source_projections,
        workspace_states,
        compiler,
    ) -> None:
        self._repository = repository
        self._source_packages = source_packages
        self._projects = projects
        self._source_projections = source_projections
        self._workspace_states = workspace_states
        self._compiler = compiler

    @staticmethod
    def plan(
        item: ReviewedRecipeApplication,
        *,
        run: MigrationRun,
        target: RunTargetBinding,
        now: datetime,
    ) -> PlannedRecipeApplication:
        """Create one isolated application and workspace plan in memory."""

        application_id = str(uuid4())
        workspace_id = str(uuid4())
        issues = item.assessment.issues
        status = (
            RecipeApplicationStatus.BLOCKED
            if item.assessment.blocked
            else RecipeApplicationStatus.DRAFT_READINESS
        )
        issue_hash = content_hash([value.to_dict() for value in issues])
        evidence_hash = content_hash(
            {
                "application_id": application_id,
                "issue_hash": issue_hash,
                "physical_binding_hash": item.assessment.physical_binding_hash,
                "recipe": item.selection.to_dict(),
                "status": status.value,
                "target_binding_hash": target.content_hash,
            }
        )
        application = RunRecipeApplication(
            application_id=application_id,
            project_id=run.project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=run.data_version_id,
            workspace_id=workspace_id,
            recipe_id=item.selection.recipe_id,
            recipe_revision=item.selection.recipe_revision,
            recipe_semantic_hash=item.selection.semantic_hash,
            target_binding_id=target.target_binding_id,
            physical_binding_hash=item.assessment.physical_binding_hash,
            parameter_values_hash=item.assessment.parameter_values_hash,
            status=status,
            issue_hash=issue_hash,
            mapping_id=None,
            mapping_content_hash=None,
            evidence_hash=evidence_hash,
            created_at=now,
            updated_at=now,
        )
        workspace = MigrationWorkspace(
            workspace_id=workspace_id,
            project_id=run.project_id,
            data_version_id=run.data_version_id,
            migration_run_id=run.migration_run_id,
            recipe_application_id=application_id,
            display_name=(
                f"{item.recipe.display_name} {run.purpose.value.title()} application"
            ),
            state=MigrationWorkspaceState.OPEN,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        return PlannedRecipeApplication(
            application=application,
            workspace=workspace,
            dataset_ids=item.assessment.dataset_ids,
            requirements=item.requirements,
            reference_requirements=item.reference_requirements,
            issues=issues,
        )

    def materialize(
        self,
        bundle: IntegratedRunBundle,
        *,
        review: IntegratedRunReview,
        operation_id: str,
        ready_event_type: str,
        target_workspace_state,
        actor: Actor,
    ) -> IntegratedRunBundle:
        """Publish source projections and fresh mapping evidence per workspace."""

        package = self._source_packages.repository.get_source_package(
            bundle.run.data_version_id
        )
        if package is None:
            raise MigrationRunPlanningError("DataVersion source package is missing")
        reviewed = {item.selection.recipe_id: item for item in review.applications}
        workspace_by_id = {item.workspace_id: item for item in bundle.workspaces}
        project = self._projects.get(bundle.run.project_id, actor=actor)
        stored_applications = []
        for application in bundle.applications:
            item = reviewed[application.recipe_id]
            workspace = workspace_by_id[application.workspace_id]
            if item.assessment.dataset_ids:
                projection = (
                    self._source_projections.repository.get_workspace_source_projection(
                        workspace.workspace_id
                    )
                )
                if projection is None:
                    self._source_projections.materialize(
                        workspace.workspace_id,
                        actor=actor,
                        dataset_ids=item.assessment.dataset_ids,
                        expected_workspace_revision=workspace.optimistic_revision,
                        operation_id=self._child_operation(
                            operation_id,
                            f"source:{application.recipe_id}",
                        ),
                    )
            self._provision_engine(
                workspace,
                project=project,
                package=package,
                target_workspace_state=target_workspace_state,
                actor=actor,
            )
            materialized = self._compiler.materialize(
                workspace.workspace_id,
                application_id=application.application_id,
                recipe_id=application.recipe_id,
                data_version_id=application.data_version_id,
                definition=item.definition,
                assessment=item.assessment,
                actor=actor,
            )
            stored_applications.append(
                self._repository.save_application_materialization(
                    application.application_id,
                    expected_evidence_hash=application.evidence_hash,
                    status=materialized.status,
                    issues=materialized.issues,
                    mapping_id=materialized.mapping_id,
                    mapping_content_hash=materialized.mapping_content_hash,
                    evidence_hash=materialized.evidence_hash,
                    actor=actor,
                )
            )
        if stored_applications and all(
            item.status is RecipeApplicationStatus.READY for item in stored_applications
        ):
            current = self._repository.foundation.get_migration_run(
                bundle.run.migration_run_id
            )
            if current.state is MigrationRunState.DRAFT:
                self._repository.foundation.save_migration_run(
                    replace(
                        current,
                        state=MigrationRunState.READY,
                        updated_at=utc_now(),
                    ),
                    expected_revision=current.optimistic_revision,
                    event_type=ready_event_type,
                    actor=actor,
                )
        return self._repository.commit_provisioning(operation_id)

    def _provision_engine(
        self,
        workspace: MigrationWorkspace,
        *,
        project,
        package: DataVersionSourcePackage,
        target_workspace_state,
        actor: Actor,
    ) -> None:
        try:
            current = self._workspace_states.repository.get(workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            source_mode = (
                SourceMode.FILE
                if package.origin is SourcePackageOrigin.FILE
                else SourceMode.ODOO
            )
            current = self._workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=source_mode,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        if target_workspace_state.odoo_connection_mode is not None and (
            current.odoo_connection_mode != target_workspace_state.odoo_connection_mode
            or current.odoo_base_url != target_workspace_state.odoo_base_url
            or current.odoo_database != target_workspace_state.odoo_database
            or current.intended_applications
            != target_workspace_state.intended_applications
            or current.intended_models != target_workspace_state.intended_models
        ):
            self._workspace_states.update_target(
                current.workspace_id,
                actor=actor,
                expected_revision=current.revision,
                odoo_connection_mode=target_workspace_state.odoo_connection_mode.value,
                odoo_base_url=target_workspace_state.odoo_base_url,
                odoo_database=target_workspace_state.odoo_database,
                intended_applications=target_workspace_state.intended_applications,
                intended_models=target_workspace_state.intended_models,
            )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))
