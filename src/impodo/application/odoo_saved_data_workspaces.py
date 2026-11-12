"""Provision isolated refresh and restore workspaces for retained Odoo data.

Saved-data orchestration calls this service before remote work. Restoration
reuses an immutable DataVersion and attaches its checked evidence; refreshing
creates a draft successor whose schema must be captured afresh by the caller.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid5

from impodo.application.data_version.service import DataVersionService
from impodo.application.data_version.source_packages import (
    WorkspaceSourceProjectionService,
)
from impodo.application.odoo_source_restart_service import OdooSourceRestartService
from impodo.application.project.service import MigrationProjectService
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationState,
    require_uuid,
)
from impodo.domain.run.models import MigrationRun, MigrationRunPurpose
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
    WorkspaceStatus,
)


class SavedOdooWorkspaceProjection(Protocol):
    """Attach checked immutable source references to a pristine workspace."""

    def attach(
        self,
        source_workspace_id: str,
        workspace_id: str,
        *,
        actor: Actor,
        expected_source_selection_hash: str | None = None,
        expected_manifest_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str, ...]: ...

    def set_source_company(
        self,
        workspace_id: str,
        selection: OdooCompanySelection,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> None: ...


class OdooSavedDataWorkspaceService:
    """Create fresh workspace roots without contacting the source Odoo."""

    def __init__(
        self,
        *,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        runs: MigrationRunService,
        workspaces: MigrationWorkspaceService,
        workspace_states: WorkspaceStateService,
        projections: WorkspaceSourceProjectionService,
        source_restarts: OdooSourceRestartService,
        projection_repository: SavedOdooWorkspaceProjection,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.workspaces = workspaces
        self.workspace_states = workspace_states
        self.projections = projections
        self.source_restarts = source_restarts
        self.projection_repository = projection_repository

    def prepare_refresh(
        self, source_workspace_id: str, *, actor: Actor, operation_id: str
    ) -> WorkspaceState:
        """Start a draft successor; the caller supplies fresh remote evidence."""

        operation_id = require_uuid(operation_id, "operation_id")
        source = self._source(source_workspace_id, actor=actor)
        if source.odoo_connection_mode is None:
            raise MigrationFoundationError("Saved Odoo source connection is missing")
        successor = self.source_restarts.start(
            source.workspace_id,
            actor=actor,
            request_id=operation_id,
            odoo_connection_mode=source.odoo_connection_mode.value,
            odoo_base_url=source.odoo_base_url,
            odoo_database=source.odoo_database,
            intended_applications=source.intended_applications,
            intended_models=source.intended_models,
        )
        return self._register_and_copy_company(
            successor.workspace_state, source, actor=actor
        )

    def prepare_restore(
        self,
        source_workspace_id: str,
        *,
        actor: Actor,
        operation_id: str,
        expected_source_selection_hash: str | None = None,
        expected_manifest_hashes: tuple[tuple[str, str], ...] = (),
    ) -> WorkspaceState:
        """Create a fresh run over retained data and project its source caches."""

        operation_id = require_uuid(operation_id, "operation_id")
        source = self._source(source_workspace_id, actor=actor)
        owner = self.workspaces.get(source.workspace_id, actor=actor)
        run = self._restore_run(
            owner, actor=actor, operation_id=self._child(operation_id, "run")
        )
        workspace = self._restore_workspace(
            owner, run, actor=actor, operation_id=self._child(operation_id, "workspace")
        )
        try:
            state = self.workspace_states.repository.get(workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            project = self.projects.get(owner.project_id, actor=actor)
            state = self.workspace_states.provision_migration_workspace(
                workspace.workspace_id,
                actor=actor,
                name=workspace.display_name,
                source_system=source.source_system,
                source_mode=SourceMode.ODOO,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        if state.status is WorkspaceStatus.DRAFT:
            if source.odoo_connection_mode is None:
                raise MigrationFoundationError(
                    "Saved Odoo source connection is missing"
                )
            state = self.workspace_states.update_target(
                state.workspace_id,
                actor=actor,
                expected_revision=state.revision,
                odoo_connection_mode=source.odoo_connection_mode.value,
                odoo_base_url=source.odoo_base_url,
                odoo_database=source.odoo_database,
                intended_applications=source.intended_applications,
                intended_models=source.intended_models,
            )
        state = self._register_and_copy_company(state, source, actor=actor)
        dataset_ids = self.projection_repository.attach(
            source.workspace_id,
            state.workspace_id,
            actor=actor,
            expected_source_selection_hash=expected_source_selection_hash,
            expected_manifest_hashes=expected_manifest_hashes,
        )
        workspace = self.workspaces.get(state.workspace_id, actor=actor)
        current_projection = (
            self.projections.repository.get_workspace_source_projection(
                state.workspace_id
            )
        )
        projection_operation = self._child(operation_id, "source-projection")
        try:
            projection_intent = self.workspaces.repository.get_operation_intent(
                projection_operation
            )
        except MigrationNotFoundError:
            projection_intent = None
        if current_projection is None or (
            projection_intent is not None
            and projection_intent.state is not MigrationOperationState.COMMITTED
        ):
            self.projections.materialize(
                state.workspace_id,
                actor=actor,
                dataset_ids=dataset_ids,
                expected_workspace_revision=(
                    projection_intent.expected_revision
                    if projection_intent is not None
                    and projection_intent.expected_revision is not None
                    else workspace.optimistic_revision
                ),
                operation_id=projection_operation,
            )
        elif (
            current_projection.data_version_id != workspace.data_version_id
            or tuple(sorted(item.dataset_id for item in current_projection.datasets))
            != dataset_ids
        ):
            raise MigrationFoundationError(
                "Restore workspace source projection changed"
            )
        return self.workspace_states.repository.get(state.workspace_id)

    def _source(self, workspace_id: str, *, actor: Actor) -> WorkspaceState:
        owner = self.workspaces.get(
            require_uuid(workspace_id, "workspace_id"), actor=actor
        )
        data = self.data_versions.get(owner.data_version_id, actor=actor)
        state = self.workspace_states.repository.get(owner.workspace_id)
        if (
            data.state is not DataVersionState.FROZEN
            or state.source_mode is not SourceMode.ODOO
        ):
            raise MigrationFoundationError("Choose a frozen Odoo source DataVersion")
        return state

    def _register_and_copy_company(
        self, state: WorkspaceState, source: WorkspaceState, *, actor: Actor
    ) -> WorkspaceState:
        if state.status is WorkspaceStatus.DRAFT:
            state = self.workspace_states.register(
                state.workspace_id, actor=actor, expected_revision=state.revision
            )
        if (
            source.odoo_company is not None
            and state.odoo_company != source.odoo_company
        ):
            self.projection_repository.set_source_company(
                state.workspace_id,
                source.odoo_company,
                actor=actor,
                expected_revision=state.revision,
            )
            state = self.workspace_states.repository.get(state.workspace_id)
        return state

    def _restore_run(
        self, source: MigrationWorkspace, *, actor: Actor, operation_id: str
    ) -> MigrationRun:
        repository = self.runs.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            project = self.projects.get(source.project_id, actor=actor)
            return self.runs.create(
                source.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=source.data_version_id,
                purpose=MigrationRunPurpose.AUTHORING,
                label="Restore saved Odoo data",
                operation_id=operation_id,
            )
        if intent.project_id != source.project_id:
            raise MigrationFoundationError(
                "Restore operation belongs to another project"
            )
        if intent.state is MigrationOperationState.COMMITTED:
            run = repository.get_migration_run(intent.owner_id)
        else:
            run = repository.resume_migration_run_creation(operation_id, actor=actor)
        if run.data_version_id != source.data_version_id:
            raise MigrationFoundationError(
                "Restore operation belongs to another DataVersion"
            )
        return run

    def _restore_workspace(
        self,
        source: MigrationWorkspace,
        run: MigrationRun,
        *,
        actor: Actor,
        operation_id: str,
    ) -> MigrationWorkspace:
        repository = self.workspaces.repository
        try:
            intent = repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            project = self.projects.get(source.project_id, actor=actor)
            return self.workspaces.create(
                source.project_id,
                actor=actor,
                expected_workspace_revision=project.optimistic_revision,
                data_version_id=source.data_version_id,
                migration_run_id=run.migration_run_id,
                display_name="Restore saved Odoo data",
                operation_id=operation_id,
            )
        if intent.project_id != source.project_id:
            raise MigrationFoundationError(
                "Restore operation belongs to another project"
            )
        if intent.state is MigrationOperationState.COMMITTED:
            workspace = repository.get_migration_workspace(intent.owner_id)
        else:
            workspace = repository.resume_migration_workspace_creation(
                operation_id, actor=actor
            )
        if (
            workspace.data_version_id != source.data_version_id
            or workspace.migration_run_id != run.migration_run_id
        ):
            raise MigrationFoundationError(
                "Restore operation has different workspace lineage"
            )
        return workspace

    @staticmethod
    def _child(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), f"saved-odoo-restore:{name}"))
