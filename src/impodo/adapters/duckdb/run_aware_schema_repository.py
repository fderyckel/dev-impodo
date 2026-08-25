"""Expose one run-owned target snapshot to isolated Recipe workspaces."""

from __future__ import annotations

from dataclasses import asdict

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

    def save_odoo_schema_check(self, workspace_id, catalog, **kwargs):
        if self.runs.get_workspace_target_schema(workspace_id) is not None:
            raise MigrationRunPlanningError(
                "Check target evidence once from the integrated run"
            )
        return self.local.save_odoo_schema_check(
            workspace_id,
            catalog,
            **kwargs,
        )

    def confirm_odoo_schema_refresh(self, workspace_id, catalog, **kwargs):
        if self.runs.get_workspace_target_schema(workspace_id) is not None:
            raise MigrationRunPlanningError(
                "Refresh target evidence once from the integrated run"
            )
        return self.local.confirm_odoo_schema_refresh(
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

    def save_run_default_projection(self, workspace_id, catalog, *, actor):
        """Store supplemental defaults only when run schema structure matches."""

        frozen = self.runs.get_workspace_target_schema(workspace_id)
        if frozen is None:
            raise MigrationRunPlanningError(
                "Recipe application target evidence is unavailable"
            )
        target_matches = all(
            (
                catalog.connection_target_hash == frozen.connection_target_hash,
                catalog.policy_hash == frozen.policy_hash,
                catalog.connection_mode == frozen.connection_mode,
                catalog.database == frozen.database,
                catalog.odoo_version == frozen.odoo_version,
                catalog.read_principal_hash == frozen.read_principal_hash,
                catalog.read_permission_hash == frozen.read_permission_hash,
                catalog.read_context_hash == frozen.read_context_hash,
                _model_structure(catalog.models) == _model_structure(frozen.models),
            )
        )
        if not target_matches:
            raise MigrationRunPlanningError(
                "Current Odoo details changed beyond create defaults; start a "
                "new run after reviewing the target change"
            )
        self.local.save_odoo_schema_catalog(workspace_id, catalog, actor=actor)


def _model_structure(models):
    """Compare target behavior while excluding labels and create defaults."""

    result = []
    for model in sorted(models, key=lambda item: item.name):
        fields = []
        for field in sorted(model.fields, key=lambda item: item.name):
            value = asdict(field)
            value.pop("label", None)
            value.pop("create_default_present", None)
            value.pop("create_default_value", None)
            fields.append(value)
        result.append(
            {
                "name": model.name,
                "fields": fields,
                "unique_constraints": [
                    asdict(item) for item in model.unique_constraints
                ],
            }
        )
    return result
