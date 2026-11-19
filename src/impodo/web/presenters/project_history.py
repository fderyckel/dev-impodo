"""Select operator workspaces, saved results and eligible Recipe sources from project records."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from impodo.application.correction_workflow import CorrectionJourneyView
from impodo.domain.data_version.models import DataVersion, DataVersionPurpose
from impodo.domain.run.models import MigrationRun, MigrationRunState
from impodo.domain.workspace.models import MigrationWorkspace, MigrationWorkspaceState


@dataclass(frozen=True, slots=True)
class ProjectAuthoringHistory:
    """Workspace choices in display order without opening their evidence stores."""

    open_workspaces: tuple[MigrationWorkspace, ...]
    saved_loads: tuple[MigrationWorkspace, ...]
    recipe_sources: tuple[MigrationWorkspace, ...]


def project_authoring_history(
    workspaces: Sequence[MigrationWorkspace],
    versions: Mapping[str, DataVersion],
    runs: Mapping[str, MigrationRun],
    corrections: Sequence[CorrectionJourneyView],
) -> ProjectAuthoringHistory:
    authoring = sorted(
        (
            item
            for item in workspaces
            if item.recipe_application_id is None
            and versions[item.data_version_id].purpose is DataVersionPurpose.AUTHORING
        ),
        key=lambda item: (item.updated_at, item.created_at, item.workspace_id),
        reverse=True,
    )
    verified = {item.completed_workspace_id for item in corrections}
    verified.update(
        item.successor_workspace_id
        for item in corrections
        if item.completed and item.successor_workspace_id is not None
    )
    open_workspaces = tuple(
        item for item in authoring if item.state is MigrationWorkspaceState.OPEN
    )
    saved_loads = sorted(
        (
            item
            for item in authoring
            if item.state is MigrationWorkspaceState.CLOSED
            and (
                item.workspace_id in verified
                or runs[item.migration_run_id].state is MigrationRunState.COMPLETED
            )
        ),
        key=lambda item: (item.closed_at or item.updated_at, item.workspace_id),
        reverse=True,
    )
    recipe_sources = tuple(
        item
        for item in authoring
        if item.state is MigrationWorkspaceState.OPEN or item.workspace_id in verified
    )
    return ProjectAuthoringHistory(open_workspaces, tuple(saved_loads), recipe_sources)
