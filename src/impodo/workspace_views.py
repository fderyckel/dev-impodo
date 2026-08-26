"""Build the explicit canonical-owner context for one workspace page."""

from __future__ import annotations

from dataclasses import dataclass

from .access import Actor, Capability
from .data_version_sources import DataVersionSourcePackage
from .domain.data_version.models import DataVersion
from .migration_foundation import (
    MigrationFoundationError,
    MigrationIdentifierConfusionError,
)
from .domain.project.models import MigrationProject
from .domain.run.models import MigrationRun
from .migration_run_setup import MigrationRunTargetSetup
from .domain.workspace.models import MigrationWorkspace
from .workspace_access import WorkspaceAccessService


@dataclass(frozen=True, slots=True)
class WorkspaceOwnerView:
    """Name every canonical owner used to render one workspace page."""

    migration_project: MigrationProject
    migration_workspace: MigrationWorkspace
    data_version: DataVersion
    migration_run: MigrationRun
    source_package: DataVersionSourcePackage
    target_setup: MigrationRunTargetSetup | None

    def __post_init__(self) -> None:
        project_id = self.migration_project.project_id
        workspace = self.migration_workspace
        if (
            workspace.project_id != project_id
            or self.data_version.project_id != project_id
            or self.data_version.data_version_id != workspace.data_version_id
            or self.migration_run.project_id != project_id
            or self.migration_run.migration_run_id != workspace.migration_run_id
            or self.migration_run.data_version_id != workspace.data_version_id
            or self.source_package.project_id != project_id
            or self.source_package.data_version_id != workspace.data_version_id
        ):
            raise MigrationIdentifierConfusionError(
                "Workspace page owners do not describe one verified lineage"
            )
        if self.target_setup is not None and (
            self.target_setup.project_id != project_id
            or self.target_setup.migration_run_id != workspace.migration_run_id
        ):
            raise MigrationIdentifierConfusionError(
                "Workspace target setup belongs to another MigrationRun"
            )

    @property
    def project_id(self) -> str:
        return self.migration_project.project_id

    @property
    def workspace_id(self) -> str:
        return self.migration_workspace.workspace_id

    @property
    def data_version_id(self) -> str:
        return self.data_version.data_version_id

    @property
    def data_version_number(self) -> int:
        return self.data_version.version_number

    @property
    def data_version_purpose(self) -> str:
        return self.data_version.purpose.value

    @property
    def migration_run_id(self) -> str:
        return self.migration_run.migration_run_id


class WorkspaceOwnerViewService:
    """Read each canonical owner after resolving one Project access context."""

    def __init__(self, repository, access: WorkspaceAccessService) -> None:
        self.repository = repository
        self.access = access

    def get(self, workspace_id: str, *, actor: Actor) -> WorkspaceOwnerView:
        context = self.access.resolve(
            workspace_id,
            actor=actor,
            capability=Capability.PROJECT_VIEW,
        )
        package = self.repository.get_source_package(context.data_version_id)
        if package is None:
            raise MigrationFoundationError(
                "The workspace DataVersion source package is missing"
            )
        return WorkspaceOwnerView(
            migration_project=self.repository.get_project(context.project_id),
            migration_workspace=self.repository.get_migration_workspace(
                context.workspace_id
            ),
            data_version=self.repository.get_data_version(context.data_version_id),
            migration_run=self.repository.get_migration_run(
                context.migration_run_id
            ),
            source_package=package,
            target_setup=self.repository.get_migration_run_target_setup(
                context.migration_run_id
            ),
        )
