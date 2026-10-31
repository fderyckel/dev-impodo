"""Reopen an existing project with the earlier development company schema."""

import duckdb

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema.business_intent_assessment import (
    BUSINESS_ASSESSMENT_TABLES,
)
from impodo.adapters.duckdb.schema.legacy_company import LEGACY_COMPANY_MIGRATION_ID
from tests.support.browser_scenarios import (
    OdooConnectionMode,
    ProjectSetupBrowserTestCase,
)
from tests.support.identity import resolve_local_actor


class LegacyCompanyProjectTests(ProjectSetupBrowserTestCase):
    def test_project_page_upgrades_legacy_layout_and_retains_saved_source_and_matches(
        self,
    ):
        workspace_id, _dataset, _key = self._mapping_ready_workspace(
            scalar_field_count=1, connection_mode=OdooConnectionMode.REMOTE
        )
        context = self.app.state.context
        owner = context.migration_workspaces.get(
            workspace_id, actor=resolve_local_actor(context)
        )
        source = context.queries.get_source_selection(workspace_id)
        mapping = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        path = (
            context.workspace_states.repository.workspace_directory(workspace_id)
            / "workspace-engine.duckdb"
        )
        with duckdb.connect(str(path)) as connection:
            for table in (*BUSINESS_ASSESSMENT_TABLES, "odoo_company_binding"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute(
                "ALTER TABLE workspace_projection_cache ADD COLUMN odoo_company_id BIGINT"
            )
            connection.execute(
                "ALTER TABLE workspace_projection_cache ADD COLUMN odoo_company_name VARCHAR DEFAULT ''"
            )
            connection.execute("UPDATE schema_version SET version = 24")
            connection.execute(
                "INSERT INTO schema_migration VALUES (23,24,?)",
                [LEGACY_COMPANY_MIGRATION_ID],
            )

        with collect_duckdb_request_timings() as timings:
            response = self.client.get(f"/projects/{owner.project_id}")
        self.assertEqual(response.status_code, 200, response.text[:1000])
        self.assertNotIn("cannot safely open", response.text)
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        self.assertEqual(
            context.mapping_workspace.mappings.get_mapping_working_draft(workspace_id),
            mapping,
        )
        self.assertIsNone(context.queries.get(workspace_id).odoo_company)
        # The page owns registry, workspace linkage, engine and Data version
        # connections. Migration reuses that engine owner, then the ordinary
        # schema validator runs once within the bounded page operation.
        self.assertLessEqual(timings.connection_count, 4)
        self.assertEqual(timings.schema_check_count, 1)
        self.assertEqual(timings.lock_retry_count, 0)
