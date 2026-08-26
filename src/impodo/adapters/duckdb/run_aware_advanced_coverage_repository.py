"""Expose run-owned reference evidence to isolated application workspaces."""

from __future__ import annotations

from impodo.domain.run.contracts import MigrationRunPlanningError
from .migration_run_planning_repository import MigrationRunPlanningRepository


class RunAwareAdvancedCoverageRepository:
    """Delegate mutable coverage state while protecting run references."""

    def __init__(self, local, runs: MigrationRunPlanningRepository) -> None:
        self.local = local
        self.runs = runs

    def get_reference_bundle(self, workspace_id):
        projection = self.runs.get_workspace_reference_bundle(workspace_id)
        if projection is not None:
            return projection
        return self.local.get_reference_bundle(workspace_id)

    def get_validated_reference_bundle(self, workspace_id):
        projection = self.runs.get_workspace_reference_bundle(workspace_id)
        if projection is not None:
            return projection
        return self.local.get_validated_reference_bundle(workspace_id)

    def save_reference_bundle(self, workspace_id, bundle, *, actor):
        if self.runs.is_application_workspace(workspace_id):
            raise MigrationRunPlanningError(
                "Refresh supporting lists once from the integrated run"
            )
        return self.local.save_reference_bundle(
            workspace_id,
            bundle,
            actor=actor,
        )

    def __getattr__(self, name):
        """Keep non-reference coverage and resolution state workspace-owned."""

        return getattr(self.local, name)
