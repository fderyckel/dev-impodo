"""Project cleanup removes owned proofs while retaining reset authority."""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.odoo_database_binding_repository import (
    DuckDbOdooDatabaseBindingRepository,
)
from impodo.adapters.duckdb.odoo_saved_data_repository import (
    DuckDbSavedOdooDataRepository,
)
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    database_scope_hash,
)
from impodo.domain.odoo_saved_data import SavedOdooDataDefinition
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.integration.duckdb import test_migration_foundation as foundation_fixtures


class DatabaseBindingCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = foundation_fixtures.MigrationFoundationTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database = self.fixture.database
        self.authority = DuckDbOdooDatabaseBindingRepository(self.database)
        self.saved = DuckDbSavedOdooDataRepository(self.database)
        self.scope_hash = database_scope_hash(
            "https://odoo.example.test", "shared-database"
        )
        self.organization_id = "shared-organization"

    def _owned_records(self, major):
        project = self.fixture._project()
        data_version = self.fixture._data_version(project)
        project = self.fixture.projects.get(project.project_id, actor=LOCAL_ACTOR)
        run = self.fixture._run(project, data_version)
        project = self.fixture.projects.get(project.project_id, actor=LOCAL_ACTOR)
        workspace = self.fixture._workspace(project, data_version, run)
        project = self.fixture.projects.get(project.project_id, actor=LOCAL_ACTOR)
        generation = self.authority.current(self.organization_id, self.scope_hash)
        bindings = []
        for role in (
            OdooDatabaseBindingRole.SOURCE,
            OdooDatabaseBindingRole.DESTINATION,
        ):
            binding = OdooDatabaseBinding(
                workspace_id=workspace.workspace_id,
                project_id=project.project_id,
                organization_id=self.organization_id,
                role=role,
                scope_hash=self.scope_hash,
                generation_hash=generation.generation_hash,
                actor=LOCAL_ACTOR.identity,
                bound_at=datetime.now(UTC),
                operation_id=str(uuid4()),
            )
            bindings.append(
                self.authority.bind(
                    binding, expected_generation_hash=generation.generation_hash
                )
            )
        now = datetime.now(UTC)
        definition = self.saved.create_definition(
            SavedOdooDataDefinition(
                saved_data_id=str(uuid4()),
                project_id=project.project_id,
                label="Retained source configuration",
                revision=1,
                source_workspace_id=workspace.workspace_id,
                source_target_hash="sha256:" + "a" * 64,
                source_company_hash="sha256:" + "b" * 64,
                odoo_major=major,
                config_json="{}",
                latest_version=None,
                created_at=now,
                updated_at=now,
            ),
            source_binding_hash=bindings[0].content_hash,
        )
        return project, workspace, tuple(bindings), definition

    def _declare_reset(self):
        before = self.authority.current(self.organization_id, self.scope_hash)
        generation = OdooDatabaseGeneration(
            organization_id=self.organization_id,
            scope_hash=self.scope_hash,
            generation=before.generation + 1,
            previous_generation_hash=before.generation_hash,
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR.identity,
            declared_at=datetime.now(UTC),
            reason="Declared database recreation",
        )
        return self.authority.declare(
            generation, expected_generation_hash=before.generation_hash
        )

    def _raw(self):
        tables = (
            "odoo_database_generation",
            "odoo_database_generation_current",
            "odoo_workspace_database_binding",
            "saved_odoo_source_binding",
            "saved_odoo_data_definition",
            "saved_odoo_data_configuration",
        )
        with self.database.connect(self.database.registry_path) as connection:
            return {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY ALL"
                ).fetchall()
                for table in tables
            }

    def test_delete_removes_only_project_owned_proofs_and_preserves_global_reset_ledger(
        self,
    ):
        project, workspace, _, definition = self._owned_records(19)
        other_project, other_workspace, other_bindings, other_definition = (
            self._owned_records(20)
        )
        declared = self._declare_reset()
        before = self._raw()

        self.fixture.projects.delete(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.optimistic_revision,
        )

        after = self._raw()
        self.assertEqual(
            after["odoo_database_generation"], before["odoo_database_generation"]
        )
        self.assertEqual(
            after["odoo_database_generation_current"],
            before["odoo_database_generation_current"],
        )
        self.assertEqual(
            self.authority.current(self.organization_id, self.scope_hash), declared
        )
        self.assertEqual(len(after["odoo_workspace_database_binding"]), 2)
        self.assertTrue(
            all(
                row[0] == other_workspace.workspace_id
                for row in after["odoo_workspace_database_binding"]
            )
        )
        self.assertEqual(
            after["saved_odoo_source_binding"],
            [(other_definition.saved_data_id, 1, other_bindings[0].content_hash)],
        )
        self.assertEqual(
            self.saved.get_definition(
                other_project.project_id, other_definition.saved_data_id
            ),
            other_definition,
        )
        self.assertFalse(self.database.project_directory(project.project_id).exists())
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_workspace WHERE workspace_id = ?",
                    [workspace.workspace_id],
                ).fetchone(),
                (0,),
            )
            # Existing cleanup reserves immutable IDs as tombstones.
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM migration_workspace_identity WHERE workspace_id = ?",
                    [workspace.workspace_id],
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM saved_odoo_data_configuration WHERE saved_data_id = ?",
                    [definition.saved_data_id],
                ).fetchone(),
                (0,),
            )

    def test_failed_delete_restores_bindings_and_configuration_with_ledger_unchanged(
        self,
    ):
        project, workspace, bindings, definition = self._owned_records(20)
        self._declare_reset()
        before = self._raw()
        with (
            patch.object(
                MigrationFoundationRepository,
                "_delete_project_registry_rows",
                side_effect=RuntimeError("final cleanup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "final cleanup failed"),
        ):
            self.fixture.projects.delete(
                project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=project.optimistic_revision,
            )
        self.assertEqual(self._raw(), before)
        self.assertTrue(self.database.project_directory(project.project_id).is_dir())
        self.assertEqual(
            self.saved.get_definition(project.project_id, definition.saved_data_id),
            definition,
        )
        for binding in bindings:
            self.assertEqual(
                self.authority.get(
                    workspace.workspace_id, binding.role, self.scope_hash
                ),
                binding,
            )


if __name__ == "__main__":
    unittest.main()
