"""Keep source-owned reads applicable while projecting canonical file evidence."""

from unittest.mock import patch

from impodo.application.data_version.source_packages import SourcePackageOrigin
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements


class WorkspaceSourceCompanyReadTests(ProjectSetupBrowserTestCase):
    def test_file_projection_uses_canonical_files_without_odoo_source_company_read(self):
        created = self.workspaces.create(name="Fictional files", source_system="CSV")
        expected = self._complete_setup_before_target(created.workspace_id)
        repository = self.app.state.context.workspace_states.repository
        with (
            patch(
                "impodo.adapters.duckdb.migration_workspace_state_repository.read_saved_source_company",
                side_effect=AssertionError("A file source has no Odoo source-company evidence"),
            ),
            count_duckdb_statements() as statements,
        ):
            projected, package = run_local_operation_sync(
                repository.get_with_source_package, created.workspace_id,
            )
        self.assertEqual(package.origin, SourcePackageOrigin.FILE)
        self.assertEqual(projected.source_files, expected.source_files)
        self.assertTrue(projected.source_files)
        self.assertFalse(any("FROM source_file" in query for query in statements))
        self.assertFalse(any("FROM odoo_saved_source_company" in query for query in statements))
        self.assertTrue(any("duckdb_columns()" in query for query in statements))

    def test_odoo_projection_retains_source_company_hash_verification(self):
        state, schema = self._frozen_odoo_source_workspace()
        repository = self.app.state.context.workspace_states.repository
        company = OdooCompanySelection(schema.connection_target_hash, 7, "Fictional source")
        path = repository.workspace_directory(state.workspace_id) / "workspace-engine.duckdb"
        with repository._connect(path) as connection:
            connection.execute(
                "INSERT INTO odoo_saved_source_company VALUES (1, ?, ?)",
                [company.binding_hash, company.to_json()],
            )
        with count_duckdb_statements() as statements:
            projected = run_local_operation_sync(repository.get, state.workspace_id)
        self.assertEqual(projected.odoo_company, company)
        self.assertIsNone(projected.destination_company)
        self.assertEqual(sum("FROM odoo_saved_source_company" in query for query in statements), 1)
        self.assertFalse(any("FROM source_file" in query for query in statements))
        with repository._connect(path) as connection:
            connection.execute(
                "UPDATE odoo_saved_source_company SET binding_hash = ?",
                ["sha256:" + "0" * 64],
            )
        with self.assertRaisesRegex(WorkspaceError, "binding changed"):
            run_local_operation_sync(repository.get, state.workspace_id)
