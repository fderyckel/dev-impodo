"""Read reviewed Odoo evidence that a run may reuse."""

from __future__ import annotations

from impodo.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.coverage import ReferenceBundle
from impodo.migration_foundation import require_uuid
from impodo.workspace_contracts import OdooSchemaCatalog


class RunTargetEvidenceUseCase:
    """Own validation of the authoring workspace's reusable Odoo evidence."""

    def __init__(
        self,
        *,
        foundation,
        compiler,
        authorization: AuthorizationPolicy,
        planning_error: type[Exception],
    ) -> None:
        self._foundation = foundation
        self._compiler = compiler
        self._authorization = authorization
        self._planning_error = planning_error

    def read(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[OdooSchemaCatalog, ReferenceBundle | None]:
        """Return Odoo 19 evidence only when it belongs to this Project."""

        project_id = require_uuid(project_id, "project_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        workspace = self._foundation.get_migration_workspace(workspace_id)
        if workspace.project_id != project_id:
            raise self._planning_error(
                "The selected Odoo evidence belongs to another Project"
            )
        self._authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        schema = self._compiler.schemas.get_odoo_schema_catalog(workspace_id)
        if schema is None or schema.origin.value != "LIVE_API":
            raise self._planning_error(
                "Capture authenticated Odoo 19 evidence in the authoring workspace first"
            )
        try:
            major = int(str(schema.odoo_version).split(".", 1)[0])
        except ValueError:
            major = -1
        if major != 19:
            raise self._planning_error(
                "The selected target evidence is not from Odoo 19"
            )
        return schema, self._compiler.references.get_reference_bundle(workspace_id)
