"""Create the first Authoring run for one Project without creating a Recipe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid5

from impodo.domain.shared.access import Actor
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackage,
    DataVersionSourcePackageService,
    SourcePackageOrigin,
    SourcePackageState,
)
from ..domain.data_version.models import DataVersion, DataVersionPurpose
from .data_version.service import DataVersionService
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationState,
    require_uuid,
)
from ..domain.project.models import MigrationProject
from .project.service import MigrationProjectService
from .run.service import MigrationRunService
from ..domain.run.models import MigrationRun, MigrationRunPurpose
from ..domain.workspace.models import MigrationWorkspace
from .workspace.service import MigrationWorkspaceService
from impodo.domain.workspace.workbench import (
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
    SourceMode,
    WorkspaceState,
)


@dataclass(frozen=True, slots=True)
class ProjectAuthoringBundle:
    """Return the distinct identities provisioned by New Project."""

    project: MigrationProject
    data_version: DataVersion
    run: MigrationRun
    workspace: MigrationWorkspace
    workspace_state: WorkspaceState


class MigrationProjectAuthoringService:
    """Coordinate restart-safe Project-native authoring initialization."""

    def __init__(
        self,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        runs: MigrationRunService,
        migration_workspaces: MigrationWorkspaceService,
        source_packages: DataVersionSourcePackageService,
        workspace_states: WorkspaceStateService,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.migration_workspaces = migration_workspaces
        self.source_packages = source_packages
        self.workspace_states = workspace_states

    def create(
        self,
        *,
        actor: Actor,
        display_name: str,
        source_mode: str | SourceMode,
        creation_request_id: str,
        migration_purpose: str = "",
        source_system_identity: str = "",
        data_classification: str = "INTERNAL",
        retention_days: int = 365,
    ) -> ProjectAuthoringBundle:
        """Create Project, DataVersion, run, and workspace as distinct roots."""

        request_id = require_uuid(creation_request_id, "creation_request_id")
        try:
            parsed_mode = SourceMode(source_mode)
        except ValueError as error:
            raise MigrationFoundationError(
                "Choose files or data already in Odoo"
            ) from error
        source_identity = source_system_identity.strip() or (
            "Uploaded files" if parsed_mode is SourceMode.FILE else "Odoo"
        )
        purpose = migration_purpose.strip() or (
            f"Prepare {display_name.strip()} data for Odoo"
        )
        project = self.projects.create(
            actor=actor,
            display_name=display_name,
            migration_purpose=purpose,
            source_system_identity=source_identity,
            data_classification=data_classification,
            retention_days=retention_days,
            operation_id=request_id,
        )

        data_version_operation = self._child_operation(
            request_id,
            "authoring-data-version",
        )
        data_version = self._data_version(
            project,
            operation_id=data_version_operation,
            actor=actor,
        )
        package = self.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            package = DataVersionSourcePackage(
                data_version_id=data_version.data_version_id,
                project_id=project.project_id,
                revision=1,
                origin=(
                    SourcePackageOrigin.FILE
                    if parsed_mode is SourceMode.FILE
                    else SourcePackageOrigin.ODOO
                ),
                state=SourcePackageState.DRAFT,
                files=(),
                catalogs=(),
                configurations=(),
                datasets=(),
                updated_at=datetime.now(timezone.utc),
            )
            self.source_packages.replace_draft(
                package,
                actor=actor,
                expected_package_revision=None,
            )

        project = self.projects.get(project.project_id, actor=actor)
        run = self._run(
            project,
            data_version,
            operation_id=self._child_operation(request_id, "authoring-run"),
            actor=actor,
        )
        project = self.projects.get(project.project_id, actor=actor)
        workspace = self._workspace(
            project,
            data_version,
            run,
            operation_id=self._child_operation(request_id, "authoring-workspace"),
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
                source_system=source_identity,
                source_mode=parsed_mode,
                data_classification=data_classification,
                retention_days=retention_days,
            )
        return ProjectAuthoringBundle(
            project=self.projects.get(project.project_id, actor=actor),
            data_version=self.data_versions.get(
                data_version.data_version_id,
                actor=actor,
            ),
            run=run,
            workspace=workspace,
            workspace_state=workspace_state,
        )

    @staticmethod
    def _child_operation(request_id: str, name: str) -> str:
        return str(uuid5(UUID(request_id), name))

    def _data_version(
        self,
        project: MigrationProject,
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
        repository = self.migration_workspaces.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.migration_workspaces.create(
                project.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=data_version.data_version_id,
                migration_run_id=run.migration_run_id,
                display_name=f"{project.display_name} mapping workspace",
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return repository.get_migration_workspace(intent.owner_id)
        return repository.resume_migration_workspace_creation(
            operation_id,
            actor=actor,
        )
