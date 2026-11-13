"""Keep source-generation CAS and historical saved artifacts atomic in DuckDB."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

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
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.integration.duckdb import test_odoo_saved_data_repository as fixtures


class SavedOdooSourceBindingRegistryTests(unittest.TestCase):
    def _fixture(self):
        fixture = fixtures.SavedOdooDataRegistryTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ledger = DuckDbOdooDatabaseBindingRepository(fixture.database)
        scope_hash = database_scope_hash("https://source.example.test", "odoo-dev")
        generation = ledger.current("fixture-organization", scope_hash)
        binding = self._bind(ledger, fixture, fixture.source.workspace_id, generation)
        return fixture, ledger, generation, binding

    def _bind(self, ledger, fixture, workspace_id, generation):
        binding = OdooDatabaseBinding(
            workspace_id=workspace_id,
            project_id=fixture.project.project_id,
            organization_id=generation.organization_id,
            role=OdooDatabaseBindingRole.SOURCE,
            scope_hash=generation.scope_hash,
            generation_hash=generation.generation_hash,
            actor=LOCAL_ACTOR.identity,
            bound_at=datetime.now(UTC),
            operation_id=str(uuid4()),
        )
        return ledger.bind(binding, expected_generation_hash=generation.generation_hash)

    def _reset(self, ledger, generation):
        replacement = OdooDatabaseGeneration(
            organization_id=generation.organization_id,
            scope_hash=generation.scope_hash,
            generation=generation.generation + 1,
            previous_generation_hash=generation.generation_hash,
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR.identity,
            declared_at=datetime.now(UTC),
            reason="The operator recreated this test database",
        )
        return ledger.declare(
            replacement, expected_generation_hash=generation.generation_hash
        )

    def _fresh_workspace(self, fixture):
        run = fixture.runs.create(
            fixture.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=fixture._project_revision(),
            data_version_id=fixture.data_version.data_version_id,
            purpose="AUTHORING",
            label="Reviewed replacement source",
        )
        return fixture.workspaces.create(
            fixture.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=fixture._project_revision(),
            data_version_id=fixture.data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Fresh source choices",
        )

    def test_reset_between_check_and_append_cannot_move_the_clean_pointer(self):
        fixture, ledger, generation, binding = self._fixture()
        fixture.repository.create_definition(
            fixture.definition, source_binding_hash=binding.content_hash
        )
        fixture._accept_fixture_data()
        first = fixture._version()
        current, _ = fixture.repository.append_version(
            first, expected_revision=1, source_binding_hash=binding.content_hash
        )
        original_definition_hash = fixture.definition.content_hash
        original_version_hash = first.content_hash
        self._reset(ledger, generation)
        with self.assertRaisesRegex(MigrationConflictError, "generation changed"):
            fixture.repository.append_version(
                fixture._version(2),
                expected_revision=current.revision,
                source_binding_hash=binding.content_hash,
            )
        reopened = DuckDbSavedOdooDataRepository(fixture.database)
        self.assertEqual(
            reopened.get_definition(current.project_id, current.saved_data_id), current
        )
        self.assertEqual(
            reopened.list_versions(current.project_id, current.saved_data_id), (first,)
        )
        self.assertEqual(
            reopened.get_configuration(
                current.project_id, current.saved_data_id, 1
            ).content_hash,
            original_definition_hash,
        )
        self.assertEqual(first.content_hash, original_version_hash)
        self.assertEqual(
            reopened.append_version(
                first, expected_revision=1, source_binding_hash=binding.content_hash
            ),
            (current, first),
        )

    def test_dedicated_rebind_preserves_versions_and_requires_current_exact_proof(self):
        fixture, ledger, generation, binding = self._fixture()
        fixture.repository.create_definition(
            fixture.definition, source_binding_hash=binding.content_hash
        )
        fixture._accept_fixture_data()
        first = fixture._version()
        current, _ = fixture.repository.append_version(
            first, expected_revision=1, source_binding_hash=binding.content_hash
        )
        generation = self._reset(ledger, generation)
        fresh = self._fresh_workspace(fixture)
        fresh_binding = self._bind(ledger, fixture, fresh.workspace_id, generation)
        candidate = replace(
            current,
            source_workspace_id=fresh.workspace_id,
            source_company_hash=content_hash("Recreated company with reviewed new ID"),
            updated_at=datetime.now(UTC),
        )
        with self.assertRaisesRegex(MigrationFoundationError, "different source"):
            fixture.repository.save_definition(
                candidate,
                expected_revision=current.revision,
                source_binding_hash=fresh_binding.content_hash,
            )
        with self.assertRaisesRegex(MigrationConflictError, "generation changed"):
            fixture.repository.rebind_source_definition(
                candidate,
                expected_revision=current.revision,
                source_binding_hash=binding.content_hash,
            )
        updated = fixture.repository.rebind_source_definition(
            candidate,
            expected_revision=current.revision,
            source_binding_hash=fresh_binding.content_hash,
        )
        self.assertEqual(
            (updated.latest_version, updated.configuration_revision), (1, 2)
        )
        self.assertEqual(
            fixture.repository.get_configuration_source_binding(
                current.project_id, current.saved_data_id, 1
            ),
            binding.content_hash,
        )
        self.assertEqual(
            fixture.repository.get_configuration_source_binding(
                current.project_id, current.saved_data_id, 2
            ),
            fresh_binding.content_hash,
        )
        self.assertEqual(
            fixture.repository.list_versions(current.project_id, current.saved_data_id),
            (first,),
        )
        with self.assertRaises(MigrationConflictError):
            fixture.repository.rebind_source_definition(
                candidate,
                expected_revision=current.revision,
                source_binding_hash=fresh_binding.content_hash,
            )


if __name__ == "__main__":
    unittest.main()
