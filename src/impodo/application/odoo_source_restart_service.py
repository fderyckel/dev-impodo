"""Start a fresh Authoring lineage when frozen Odoo source scope changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid5

from impodo.application.data_version.service import DataVersionService
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    DataVersionSourcePackageService,
    SourcePackageOrigin,
    SourcePackageState,
)
from impodo.application.project.service import MigrationProjectService
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.domain.data_version.models import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
)
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationState,
    require_uuid,
)
from impodo.domain.project.models import MigrationProject
from impodo.domain.run.models import MigrationRun, MigrationRunPurpose
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
)


@dataclass(frozen=True, slots=True)
class OdooSourceSuccessor:
    """Return the fresh roots created for one frozen-source restart."""

    project: MigrationProject
    data_version: DataVersion
    run: MigrationRun
    workspace: MigrationWorkspace
    workspace_state: WorkspaceState


class OdooSourceRestartService:
    """Create an idempotent successor without rewriting frozen source evidence."""

    def __init__(
        self,
        *,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        runs: MigrationRunService,
        workspaces: MigrationWorkspaceService,
        source_packages: DataVersionSourcePackageService,
        workspace_states: WorkspaceStateService,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.workspaces = workspaces
        self.source_packages = source_packages
        self.workspace_states = workspace_states

    def start(
        self,
        predecessor_workspace_id: str,
        *,
        actor: Actor,
        request_id: str,
        odoo_connection_mode: str,
        odoo_base_url: str,
        odoo_database: str,
        intended_applications: tuple[str, ...],
        intended_models: tuple[str, ...] | None = None,
    ) -> OdooSourceSuccessor:
        """Create or resume one clean Odoo-source Authoring successor."""

        predecessor_workspace_id = require_uuid(
            predecessor_workspace_id,
            "predecessor_workspace_id",
        )
        request_id = require_uuid(request_id, "request_id")
        predecessor = self.workspaces.get(
            predecessor_workspace_id,
            actor=actor,
        )
        predecessor_data = self.data_versions.get(
            predecessor.data_version_id,
            actor=actor,
        )
        predecessor_run = self.runs.get(
            predecessor.migration_run_id,
            actor=actor,
        )
        predecessor_state = self.workspace_states.repository.get(
            predecessor_workspace_id
        )
        if predecessor_state.source_mode is not SourceMode.ODOO:
            raise MigrationFoundationError(
                "Only an Odoo-source workspace can start an Odoo source successor"
            )
        if predecessor_data.state is not DataVersionState.FROZEN:
            raise MigrationFoundationError(
                "Only accepted Odoo source data requires a successor DataVersion"
            )
        if (
            predecessor_data.purpose is not DataVersionPurpose.AUTHORING
            or predecessor_run.purpose is not MigrationRunPurpose.AUTHORING
        ):
            raise MigrationFoundationError(
                "Only an Authoring Odoo source can restart from Source data"
            )

        project = self.projects.get(predecessor.project_id, actor=actor)
        data_version = self._data_version(
            project,
            predecessor_data,
            operation_id=self._child_operation(request_id, "source-data-version"),
            actor=actor,
        )
        package = self.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            self.source_packages.replace_draft(
                DataVersionSourcePackage(
                    data_version_id=data_version.data_version_id,
                    project_id=project.project_id,
                    revision=1,
                    origin=SourcePackageOrigin.ODOO,
                    state=SourcePackageState.DRAFT,
                    files=(),
                    catalogs=(),
                    configurations=(),
                    datasets=(),
                    updated_at=datetime.now(timezone.utc),
                ),
                actor=actor,
                expected_package_revision=None,
            )
        elif (
            package.origin is not SourcePackageOrigin.ODOO
            or package.state is not SourcePackageState.DRAFT
        ):
            raise MigrationFoundationError(
                "The successor source package is not an editable Odoo package"
            )

        project = self.projects.get(project.project_id, actor=actor)
        run = self._run(
            project,
            data_version,
            operation_id=self._child_operation(request_id, "source-run"),
            actor=actor,
        )
        project = self.projects.get(project.project_id, actor=actor)
        workspace = self._workspace(
            project,
            data_version,
            run,
            operation_id=self._child_operation(request_id, "source-workspace"),
            actor=actor,
        )
        try:
            workspace_state = self.workspace_states.repository.get(
                workspace.workspace_id
            )
        except WorkspaceStateNotFoundError:
            workspace_state = self.workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=predecessor_state.source_system,
                source_mode=SourceMode.ODOO,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        if self.workspace_states.target_update_changes(
            workspace.workspace_id,
            actor=actor,
            expected_revision=workspace_state.revision,
            odoo_connection_mode=odoo_connection_mode,
            odoo_base_url=odoo_base_url,
            odoo_database=odoo_database,
            intended_applications=intended_applications,
            intended_models=intended_models,
        ):
            workspace_state = self.workspace_states.update_target(
                workspace.workspace_id,
                actor=actor,
                expected_revision=workspace_state.revision,
                odoo_connection_mode=odoo_connection_mode,
                odoo_base_url=odoo_base_url,
                odoo_database=odoo_database,
                intended_applications=intended_applications,
                intended_models=intended_models,
            )
        return OdooSourceSuccessor(
            project=self.projects.get(project.project_id, actor=actor),
            data_version=self.data_versions.get(
                data_version.data_version_id,
                actor=actor,
            ),
            run=self.runs.get(run.migration_run_id, actor=actor),
            workspace=self.workspaces.get(workspace.workspace_id, actor=actor),
            workspace_state=workspace_state,
        )

    @staticmethod
    def _child_operation(request_id: str, name: str) -> str:
        return str(uuid5(UUID(request_id), name))

    def _data_version(
        self,
        project: MigrationProject,
        predecessor: DataVersion,
        *,
        operation_id: str,
        actor: Actor,
    ) -> DataVersion:
        repository = self.data_versions.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.data_versions.create(
                project.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                purpose=DataVersionPurpose.AUTHORING,
                label=f"{project.display_name} authoring data",
                parent_data_version_id=predecessor.data_version_id,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return repository.get_data_version(intent.owner_id)
        return repository.resume_data_version_creation(operation_id, actor=actor)

    def _run(
        self,
        project: MigrationProject,
        data_version: DataVersion,
        *,
        operation_id: str,
        actor: Actor,
    ) -> MigrationRun:
        repository = self.runs.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.runs.create(
                project.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data_version.data_version_id,
                purpose=MigrationRunPurpose.AUTHORING,
                label=f"{project.display_name} authoring run",
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return repository.get_migration_run(intent.owner_id)
        return repository.resume_migration_run_creation(operation_id, actor=actor)

    def _workspace(
        self,
        project: MigrationProject,
        data_version: DataVersion,
        run: MigrationRun,
        *,
        operation_id: str,
        actor: Actor,
    ) -> MigrationWorkspace:
        repository = self.workspaces.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.workspaces.create(
                project.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data_version.data_version_id,
                migration_run_id=run.migration_run_id,
                display_name=f"{project.display_name} source restart",
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return repository.get_migration_workspace(intent.owner_id)
        return repository.resume_migration_workspace_creation(
            operation_id,
            actor=actor,
        )


def odoo_source_restart_request_id(
    workspace_id: str,
    *,
    workspace_revision: int,
    odoo_connection_mode: str,
    odoo_base_url: str,
    odoo_database: str,
    intended_applications: tuple[str, ...],
    intended_models: tuple[str, ...] | None,
) -> str:
    """Derive one retry-stable command ID from the submitted source change."""

    workspace_id = require_uuid(workspace_id, "workspace_id")
    payload_hash = content_hash(
        {
            "intended_applications": intended_applications,
            "intended_models": intended_models,
            "odoo_base_url": odoo_base_url.strip(),
            "odoo_connection_mode": odoo_connection_mode.strip(),
            "odoo_database": odoo_database.strip(),
            "workspace_revision": workspace_revision,
        }
    )
    return str(
        uuid5(
            UUID(workspace_id),
            f"odoo-source-restart:{payload_hash}",
        )
    )
