"""Select and clone the source company for reusable Odoo captures."""

from __future__ import annotations

from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.project.foundation import utc_now
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStateConflictError

from .repository import DuckDbRepository
from .schema.odoo_saved_source_company import read_saved_source_company


class OdooSavedSourceCompanyRepository(DuckDbRepository):
    """Keep source context and its evidence invalidation in one transaction."""

    def set_source_company(
        self,
        workspace_id: str,
        selection: OdooCompanySelection,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> None:
        """Select source context before capture, preserving the destination."""

        self._assert_workspace_mutable(workspace_id)
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    "SELECT revision, source_mode, odoo_connection_mode, "
                    "odoo_base_url, odoo_database FROM workspace_projection_cache"
                ).fetchone()
                if row is None or str(row[1]) != SourceMode.ODOO.value:
                    raise WorkspaceError("Choose an Odoo-source workspace")
                if int(row[0]) != expected_revision:
                    raise WorkspaceStateConflictError(
                        "The source workspace changed; reload it"
                    )
                if selection.target_hash != target_identity_hash(
                    connection_mode=str(row[2]),
                    base_url=str(row[3]),
                    database=str(row[4]),
                ):
                    raise WorkspaceError(
                        "The source company belongs to another database"
                    )
                existing = read_saved_source_company(connection)
                if existing == selection:
                    connection.commit()
                    return
                if (
                    connection.execute(
                        "SELECT 1 FROM source_selection LIMIT 1"
                    ).fetchone()
                    is not None
                ):
                    raise WorkspaceError(
                        "Start a fresh capture before changing its source company"
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO odoo_saved_source_company VALUES (1, ?, ?)",
                    [selection.binding_hash, selection.to_json()],
                )
                for table in (
                    "odoo_schema_catalog",
                    "odoo_model_catalog",
                    "odoo_capture_selection_current",
                    "odoo_capture_manifest_current",
                    "odoo_relationship_scope_current",
                    "source_snapshot_current",
                    "schema_governance_current",
                    "derived_entity_plan_current",
                    "mapping_current",
                ):
                    connection.execute(f"DELETE FROM {table}")
                self._invalidate_canonical_staging(
                    connection, reason="SAVED_ODOO_SOURCE_COMPANY_CHANGED"
                )
                connection.execute(
                    "UPDATE workspace_projection_cache SET revision = ?, updated_at = ?",
                    [expected_revision + 1, utc_now().isoformat()],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=expected_revision + 1,
                    event_type="SAVED_ODOO_SOURCE_COMPANY_SELECTED",
                    detail="Source company selected; destination context unchanged",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
