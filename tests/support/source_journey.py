"""Provide accepted fictional source rows and Odoo choices for Stage 1 journeys."""

from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.contracts import OdooModelCatalog, OdooModelSummary
from tests.support.browser_scenarios import POST_HEADERS, _created_workspace_id
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class SourceJourneyFixture(HierarchyWorkflowFixture):
    """Use real source persistence without reading a user's files or Odoo server."""

    def inspect_files(self):
        """Stop after inspection so a browser can confirm and accept each file."""
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Workshop source files",
                "source_mode": "FILE",
                "source_system_identity": "Fictional workshop",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files=[
                (
                    "source_file",
                    (
                        "products.csv",
                        b"Reference,Name\nP1,Hammer\nP2,Drill\n",
                        "text/csv",
                    ),
                ),
                (
                    "source_file",
                    ("categories.csv", b"Code,Name\nT,Tools\n", "text/csv"),
                ),
            ],
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {
                "csrf_token": self.csrf,
                "revision": str(self.context.queries.get(workspace_id).revision),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        self.assertEqual(len(self.context.queries.get_source_catalogs(workspace_id)), 2)
        self.assertIsNone(self.context.queries.get_source_selection(workspace_id))
        return workspace_id

    def configure(self, major=20, mode="LOCAL"):
        workspace_id = self.freeze_files(
            {
                "products": b"Id,Group,Subgroup,Name\nP1,Tools,Hand,Hammer\nP2,Tools,Power,Drill\n",
                "details": b"Id,City\nP1,Brussels\nP2,Ghent\n",
            }
        )
        schema = self.schema(workspace_id, major=major, mode=mode)
        catalog = OdooModelCatalog(
            workspace_id=workspace_id,
            connection_target_hash=schema.connection_target_hash,
            policy_hash=odoo_source_policy_hash(major),
            captured_at=schema.captured_at,
            captured_by=schema.captured_by,
            connection_mode=mode,
            database=schema.database,
            odoo_version=f"{major}.0",
            models=(
                OdooModelSummary("product.category", "Category", ("base",), "base"),
            ),
            content_hash="sha256:" + "8" * 64,
            read_credential_binding_hash=schema.read_credential_binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            read_context_hash=schema.read_context_hash,
        )
        self.context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_id, catalog, actor=self.actor
        )
        self.workspace_id = workspace_id
        self.selection = self.context.queries.get_source_selection(workspace_id)
        return workspace_id, self.selection
