"""Create and activate fresh Project Production runs from qualified plans."""

from __future__ import annotations

from typing import Mapping
from uuid import UUID, uuid5

from ..access import Actor, AuthorizationPolicy, Capability
from ..data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    SourcePackageState,
)
from ..data_versions import DataVersionPurpose, DataVersionState
from ..domain.serialization import content_hash
from ..migration_foundation import (
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ..migration_production import (
    ProductionRunBinding,
    ProductionRunBindingState,
    ProductionRunError,
    ProductionRunSetupBundle,
)
from ..migration_runs import MigrationRunPurpose
from ..models import OdooReadIdentity, OdooWriteIdentity
from ..odoo_scope import OdooApiScope, OdooModelScope
from ..workspace_state import WorkspaceStateNotFoundError, SourceMode


class ProductionCutoverService:
    """Own setup, activation, and execution guards for latest-data rollout."""

    def __init__(
        self,
        *,
        projects,
        data_versions,
        runs,
        migration_workspaces,
        source_packages,
        workspace_states,
        cutover_plans,
        production_runs,
        run_planning,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.migration_workspaces = migration_workspaces
        self.source_packages = source_packages
        self.workspace_states = workspace_states
        self.cutover_plans = cutover_plans
        self.production_runs = production_runs
        self.run_planning = run_planning
        self.authorization = authorization

    def start_setup(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        cutover_selection_id: str,
        label: str,
        export_as_of: str,
        operation_id: str,
        actor: Actor,
    ) -> ProductionRunSetupBundle:
        """Create fresh data and setup identities without write authority."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        cutover_selection_id = require_uuid(
            cutover_selection_id,
            "cutover_selection_id",
        )
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        clean_export_as_of = required_text(
            export_as_of,
            "export_as_of",
            maximum=100,
        )
        self.authorization.require(
            actor,
            Capability.PRODUCTION_RUN_CREATE,
            project_id=project_id,
        )
        replay = self._committed_setup(
            project_id,
            operation_id=operation_id,
            cutover_selection_id=cutover_selection_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            actor=actor,
        )
        if replay is not None:
            return replay

        selection = self.cutover_plans.get_selection(cutover_selection_id)
        current_selection = self.cutover_plans.current_selection(project_id)
        if selection.project_id != project_id or current_selection != selection:
            raise ProductionRunError(
                "Choose the current Project rollout candidate before starting Production"
            )
        qualification = self.cutover_plans.get_qualification(
            selection.qualification_id
        )
        self.cutover_plans.assert_qualification_authentic(qualification)
        plan = self.cutover_plans.get_revision(
            selection.cutover_plan_id,
            selection.cutover_plan_revision,
        )
        plan_root = self.cutover_plans.get_plan(selection.cutover_plan_id)
        if (
            plan.project_id != project_id
            or plan.content_hash != qualification.plan_content_hash
            or qualification.cutover_plan_revision != plan.version
            or plan_root.current_revision != plan.version
        ):
            raise ProductionRunError(
                "The selected qualification no longer matches the current CutoverPlan"
            )
        test_run = self.runs.repository.get_migration_run(qualification.test_run_id)
        test_package = self.source_packages.repository.get_source_package(
            test_run.data_version_id
        )
        test_data_version = self.data_versions.repository.get_data_version(
            test_run.data_version_id
        )
        if (
            test_package is None
            or test_package.state is not SourcePackageState.FROZEN
            or test_data_version.state is not DataVersionState.FROZEN
            or test_data_version.source_package_hash != test_package.content_hash
        ):
            raise ProductionRunError(
                "The qualified Test data source evidence is missing"
            )
        if test_package.origin is not SourcePackageOrigin.FILE:
            raise ProductionRunError(
                "Production rollout currently requires a file-source CutoverPlan"
            )

        data_operation = self._child_operation(operation_id, "production-data")
        data_version = self._data_version(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            parent_data_version_id=test_run.data_version_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            operation_id=data_operation,
            actor=actor,
        )
        package = self.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            package = DataVersionSourcePackage(
                data_version_id=data_version.data_version_id,
                project_id=project_id,
                revision=1,
                origin=test_package.origin,
                state=SourcePackageState.DRAFT,
                files=(),
                catalogs=(),
                configurations=(),
                datasets=(),
                updated_at=data_version.created_at,
            )
            self.source_packages.replace_draft(
                package,
                actor=actor,
                expected_package_revision=None,
            )

        project = self.projects.get(project_id, actor=actor)
        run = self._run(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            label=clean_label,
            operation_id=self._child_operation(operation_id, "production-run"),
            actor=actor,
        )
        project = self.projects.get(project_id, actor=actor)
        setup_workspace = self._workspace(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            label=f"{clean_label} data and target setup",
            operation_id=self._child_operation(operation_id, "production-setup-workspace"),
            actor=actor,
        )
        try:
            self.workspace_states.repository.get(setup_workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            project = self.projects.get(project_id, actor=actor)
            self.workspace_states.provision_migration_workspace(
                setup_workspace.workspace_id,
                actor=actor,
                name=setup_workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=SourceMode.FILE,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )

        binding = ProductionRunBinding(
            production_run_binding_id=self._child_operation(
                operation_id,
                "production-binding",
            ),
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=data_version.data_version_id,
            setup_workspace_id=setup_workspace.workspace_id,
            cutover_selection_id=selection.cutover_selection_id,
            qualification_id=qualification.qualification_id,
            cutover_plan_id=plan.cutover_plan_id,
            cutover_plan_revision=plan.version,
            plan_content_hash=plan.content_hash,
            test_target_binding_hash=qualification.target_binding_hash,
            state=ProductionRunBindingState.SETUP,
            target_binding_id=None,
            read_credential_generation=None,
            write_credential_generation=None,
            write_principal_hash=None,
            write_permission_hash=None,
            write_context_hash=None,
            parameter_values_hash=None,
            control_values_hash=None,
            activation_evidence_hash=None,
            created_at=utc_now(),
        )
        project = self.projects.get(project_id, actor=actor)
        stored = self.production_runs.bind_setup(
            binding,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=self._child_operation(operation_id, "bind-production-setup"),
            request_hash=content_hash(
                {
                    "cutover_selection_id": cutover_selection_id,
                    "export_as_of": data_version.export_as_of,
                    "label": clean_label,
                    "production_binding": binding.to_dict(),
                    "project_id": project_id,
                }
            ),
            actor=actor,
        )
        return ProductionRunSetupBundle(
            data_version=self.data_versions.get(
                data_version.data_version_id,
                actor=actor,
            ),
            run=self.runs.get(run.migration_run_id, actor=actor),
            setup_workspace=self.migration_workspaces.get(
                setup_workspace.workspace_id,
                actor=actor,
            ),
            binding=stored,
        )

    def activate(
        self,
        project_id: str,
        migration_run_id: str,
        *,
        expected_workspace_revision: int,
        target_schema,
        target_reference_bundle,
        read_credential_generation: str,
        write_identity: OdooWriteIdentity,
        write_credential_generation: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        operation_id: str,
        actor: Actor,
        fault=None,
    ):
        """Activate exact selected meaning with fresh Production evidence."""

        project_id = require_uuid(project_id, "project_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        binding = self.production_runs.get(migration_run_id)
        if binding.project_id != project_id:
            raise ProductionRunError("Production run does not belong to this Project")
        selection = self.cutover_plans.get_selection(binding.cutover_selection_id)
        current_selection = self.cutover_plans.current_selection(project_id)
        if selection != current_selection:
            raise ProductionRunError(
                "The Project rollout candidate changed; start a new Production run"
            )
        qualification = self.cutover_plans.get_qualification(
            binding.qualification_id
        )
        self.cutover_plans.assert_qualification_authentic(qualification)
        plan = self.cutover_plans.get_revision(
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
        )
        if (
            self.cutover_plans.get_plan(binding.cutover_plan_id).current_revision
            != binding.cutover_plan_revision
        ):
            raise ProductionRunError(
                "The CutoverPlan changed; qualify and select its current revision"
            )
        test_target = self.run_planning.repository.get_target_binding(
            qualification.test_run_id
        )
        return self.run_planning.activate_production_run(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            production_binding=binding,
            plan=plan,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            test_connection_target_hash=test_target.connection_target_hash,
            read_credential_generation=read_credential_generation,
            write_identity=write_identity,
            write_credential_generation=write_credential_generation,
            parameter_values=parameter_values,
            control_values=control_values,
            shared_control_values={
                "control:project.integrated_reconciliation": False,
                "control:project.package_completeness": True,
            },
            operation_id=operation_id,
            actor=actor,
            fault=fault,
        )

    def write_scope(self, migration_run_id: str) -> OdooApiScope:
        """Build one bounded activation probe from qualified write ownership."""

        binding = self.production_runs.get(
            require_uuid(migration_run_id, "migration_run_id")
        )
        plan = self.cutover_plans.get_revision(
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
        )
        by_model: dict[str, set[str]] = {}
        for owner in plan.write_ownership:
            by_model.setdefault(owner.model, set()).add(owner.field)
        return OdooApiScope(
            preview_hash=plan.content_hash,
            models=tuple(
                OdooModelScope(
                    model=model,
                    write_fields=tuple(sorted(fields)),
                    read_fields=tuple(sorted(fields)),
                )
                for model, fields in sorted(by_model.items())
            ),
        )

    def assert_execution_authority(
        self,
        workspace_id: str,
        *,
        read_identity: OdooReadIdentity | None,
        read_credential_generation: str,
        expected_read_credential_generation: str,
        write_identity: OdooWriteIdentity | None,
        write_credential_generation: str,
        actor: Actor,
    ) -> None:
        """Reject stale Production authority before constructing an Odoo writer."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        binding = self.production_runs.for_workspace(workspace_id)
        if binding is None:
            return
        self.authorization.require(
            actor,
            Capability.PRODUCTION_RUN_ACTIVATE,
            project_id=binding.project_id,
        )
        if binding.state is not ProductionRunBindingState.ACTIVE:
            raise ProductionRunError(
                "Complete Production activation before loading data into Odoo"
            )
        current_selection = self.cutover_plans.current_selection(binding.project_id)
        if (
            current_selection is None
            or current_selection.cutover_selection_id
            != binding.cutover_selection_id
        ):
            raise ProductionRunError(
                "The rollout candidate changed; this Production run cannot write"
            )
        plan = self.cutover_plans.get_revision(
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
        )
        plan_root = self.cutover_plans.get_plan(binding.cutover_plan_id)
        if (
            plan.content_hash != binding.plan_content_hash
            or plan_root.current_revision != binding.cutover_plan_revision
        ):
            raise ProductionRunError(
                "The qualified CutoverPlan changed; this Production run cannot write"
            )
        qualification = self.cutover_plans.get_qualification(
            binding.qualification_id
        )
        self.cutover_plans.assert_qualification_authentic(qualification)
        data_version = self.data_versions.repository.get_data_version(
            binding.data_version_id
        )
        if (
            data_version.purpose is not DataVersionPurpose.PRODUCTION
            or data_version.state is not DataVersionState.FROZEN
        ):
            raise ProductionRunError(
                "The accepted Production data version is no longer current"
            )
        target = self.run_planning.repository.get_target_binding(
            binding.migration_run_id
        )
        if (
            read_identity is None
            or write_identity is None
            or read_identity.target_hash != target.connection_target_hash
            or write_identity.target_hash != target.connection_target_hash
            or read_identity.principal_hash != target.principal_hash
            or read_identity.permission_hash != target.permission_hash
            or read_identity.context_hash != target.context_hash
            or write_identity.principal_hash != binding.write_principal_hash
            or write_identity.permission_hash != binding.write_permission_hash
            or write_identity.context_hash != binding.write_context_hash
            or not read_credential_generation
            or read_credential_generation != expected_read_credential_generation
            or not write_credential_generation
        ):
            raise ProductionRunError(
                "Production target or identity changed, or comparison used an "
                "older read key; refresh comparison, and start a new Production "
                "setup if the identity or context changed"
            )

    def credential_workspace(self, workspace_id: str, *, actor: Actor):
        """Return the one vault owner for a Production setup or application."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        binding = self.production_runs.for_workspace(workspace_id)
        if binding is None:
            return self.workspace_states.repository.get(workspace_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return self.workspace_states.repository.get(binding.setup_workspace_id)

    def _committed_setup(
        self,
        project_id: str,
        *,
        operation_id: str,
        cutover_selection_id: str,
        label: str,
        export_as_of: str,
        actor: Actor,
    ) -> ProductionRunSetupBundle | None:
        bind_operation = self._child_operation(operation_id, "bind-production-setup")
        try:
            intent = self.production_runs.foundation.get_operation_intent(
                bind_operation
            )
        except MigrationNotFoundError:
            return None
        if (
            intent.kind is not MigrationOperationKind.PRODUCTION_RUN_SETUP
            or intent.project_id != project_id
            or intent.actor.issuer != actor.identity.issuer
            or intent.actor.subject_id != actor.identity.subject_id
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different meaning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        binding = self.production_runs.get(intent.owner_id)
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        run = self.runs.get(binding.migration_run_id, actor=actor)
        if (
            binding.cutover_selection_id != cutover_selection_id
            or data_version.label != label
            or data_version.export_as_of != export_as_of.strip()
        ):
            raise MigrationConflictError(
                "Operation identity was already used for another Production setup"
            )
        return ProductionRunSetupBundle(
            data_version=data_version,
            run=run,
            setup_workspace=self.migration_workspaces.get(
                binding.setup_workspace_id,
                actor=actor,
            ),
            binding=binding,
        )

    def _data_version(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        parent_data_version_id: str,
        label: str,
        export_as_of: str,
        operation_id: str,
        actor: Actor,
    ):
        try:
            intent = self.data_versions.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.data_versions.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                purpose=DataVersionPurpose.PRODUCTION,
                label=label,
                export_as_of=export_as_of,
                parent_data_version_id=parent_data_version_id,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.data_versions.repository.get_data_version(intent.owner_id)
        return self.data_versions.repository.resume_data_version_creation(
            operation_id,
            actor=actor,
        )

    def _run(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        data_version_id: str,
        label: str,
        operation_id: str,
        actor: Actor,
    ):
        try:
            intent = self.runs.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.runs.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                purpose=MigrationRunPurpose.PRODUCTION,
                label=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.runs.repository.get_migration_run(intent.owner_id)
        return self.runs.repository.resume_migration_run_creation(
            operation_id,
            actor=actor,
        )

    def _workspace(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        data_version_id: str,
        migration_run_id: str,
        label: str,
        operation_id: str,
        actor: Actor,
    ):
        try:
            intent = self.migration_workspaces.repository.get_operation_intent(
                operation_id
            )
        except MigrationNotFoundError:
            return self.migration_workspaces.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                migration_run_id=migration_run_id,
                display_name=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.migration_workspaces.repository.get_migration_workspace(
                intent.owner_id
            )
        return self.migration_workspaces.repository.resume_migration_workspace_creation(
            operation_id,
            actor=actor,
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))

