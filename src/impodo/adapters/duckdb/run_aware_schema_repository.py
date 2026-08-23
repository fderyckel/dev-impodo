"""Expose one run-owned target snapshot to isolated Recipe workspaces."""

from __future__ import annotations

from ...migration_run_planning import MigrationRunPlanningError
from .migration_run_planning_repository import MigrationRunPlanningRepository


class RunAwareSchemaRepository:
    """Delegate authoring writes and project run-level application reads."""

    def __init__(self, local, runs: MigrationRunPlanningRepository) -> None:
        self.local = local
        self.runs = runs

    def get_odoo_schema_catalog(self, workspace_id):
        current = self.local.get_odoo_schema_catalog(workspace_id)
        if current is not None:
            return current
        return self.runs.get_workspace_target_schema(workspace_id)

    def get_odoo_model_catalog(self, workspace_id):
        return self.local.get_odoo_model_catalog(workspace_id)

    def save_odoo_model_catalog(self, workspace_id, catalog, *, actor):
        if self.runs.get_workspace_target_schema(workspace_id) is not None:
            raise MigrationRunPlanningError(
                "This application uses its MigrationRun target evidence"
            )
        return self.local.save_odoo_model_catalog(workspace_id, catalog, actor=actor)

    def save_odoo_schema_catalog(self, workspace_id, catalog, *, actor):
        if self.runs.get_workspace_target_schema(workspace_id) is not None:
            raise MigrationRunPlanningError(
                "Refresh target evidence once from the integrated run"
            )
        return self.local.save_odoo_schema_catalog(workspace_id, catalog, actor=actor)

    def rebind_odoo_schema_access(self, workspace_id, catalog, **kwargs):
        if self.runs.get_workspace_target_schema(workspace_id) is not None:
            raise MigrationRunPlanningError(
                "Refresh target evidence once from the integrated run"
            )
        return self.local.rebind_odoo_schema_access(
            workspace_id,
            catalog,
            **kwargs,
        )

    def get_schema_governance(self, workspace_id):
        return self.local.get_schema_governance(workspace_id)

    def save_schema_governance(self, workspace_id, governance, *, actor):
        projection = self.runs.get_workspace_target_schema(workspace_id)
        if (
            projection is not None
            and self.local.get_odoo_schema_catalog(workspace_id) is None
        ):
            self.local.save_odoo_schema_catalog(
                workspace_id,
                projection,
                actor=actor,
            )
        return self.local.save_schema_governance(
            workspace_id,
            governance,
            actor=actor,
        )
