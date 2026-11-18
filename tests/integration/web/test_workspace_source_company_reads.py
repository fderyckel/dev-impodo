"""Keep source-owned reads applicable while projecting canonical file evidence."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from impodo.application.data_version.source_packages import (
    SourcePackageOrigin,
    SourcePackageState,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.project.foundation import MigrationConflictError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class WorkspaceSourceCompanyReadTests(ProjectSetupBrowserTestCase):
    def test_verified_source_reuses_the_package_and_reads_the_current_target(self):
        workspace_id, dataset, _ = self._mapping_ready_workspace(scalar_field_count=1)
        context = self.app.state.context
        repository = context.workspace_states.repository
        foundation = repository.foundation
        workspace = foundation.get_migration_workspace(workspace_id)
        target = foundation.get_migration_run_target_setup(workspace.migration_run_id)
        changed = replace(target, database="changed-target", revision=target.revision + 1)

        def read_state():
            with patch.object(
                foundation, "get_source_package", wraps=foundation.get_source_package,
            ) as read_package:
                selection, package = context.queries.get_mapping_source_selection_with_package(
                    workspace_id,
                )
                self.assertEqual(
                    [item.dataset_id for item in selection.datasets], [dataset.dataset_id],
                )
                foundation.replace_migration_run_target_setup(
                    changed, expected_revision=target.revision, actor=resolve_local_actor(context),
                )
                projected = context.queries.get_with_verified_source(workspace_id, package)
                read_package.assert_called_once_with(workspace.data_version_id)
                read_package.reset_mock()
                self.assertEqual(repository.get(workspace_id), projected)
                read_package.assert_called_once_with(workspace.data_version_id)
            return projected

        state = run_local_operation_sync(read_state)
        self.assertEqual(state.odoo_database, changed.database)
        self.assertTrue(state.source_files)

    def test_verified_source_state_rejects_foreign_or_draft_packages(self):
        workspace_id, _, _ = self._mapping_ready_workspace(scalar_field_count=1)
        repository = self.app.state.context.workspace_states.repository
        _, package = repository.get_with_source_package(workspace_id)
        for reason, invalid in (
            ("other project", replace(package, project_id=str(uuid4()))),
            ("other DataVersion", replace(package, data_version_id=str(uuid4()))),
            ("draft", replace(package, state=SourcePackageState.DRAFT, frozen_at=None)),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(
                MigrationConflictError, "frozen source package from its own DataVersion",
            ):
                repository.get_with_source_package(workspace_id, verified_package=invalid)

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
