"""Keep reset declarations atomic across Projects and preserve old workspace proof."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.odoo_database_binding_repository import (
    DuckDbOdooDatabaseBindingRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema import migration_registry
from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.unit_of_work import retain_databases_for_operation
from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    OdooDatabaseGenerationConflict,
    database_scope_hash,
)
from impodo.domain.project.foundation import MigrationStorageCompatibilityError
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.integration.duckdb import test_migration_foundation as foundation_fixtures
from tests.support.database_probe import count_duckdb_statements


class OdooDatabaseBindingRepositoryTests(TestCase):
    def setUp(self):
        self.foundation = foundation_fixtures.MigrationFoundationTests(
            methodName="runTest"
        )
        self.foundation.setUp()
        self.addCleanup(self.foundation.doCleanups)
        self.database = self.foundation.database
        self.repository = DuckDbOdooDatabaseBindingRepository(self.database)
        self.now = datetime.now(UTC)
        self.scope = database_scope_hash("https://odoo.example.test", "demo")
        self.project, self.workspace = self.create_workspace()

    def create_workspace(self, project=None):
        project = project or self.foundation._project()
        data = self.foundation._data_version(
            self.foundation.repository.get_project(project.project_id)
        )
        run = self.foundation._run(
            self.foundation.repository.get_project(project.project_id), data
        )
        workspace = self.foundation._workspace(
            self.foundation.repository.get_project(project.project_id), data, run
        )
        return self.foundation.repository.get_project(project.project_id), workspace

    def binding(
        self,
        workspace=None,
        project=None,
        *,
        organization="organization-a",
        role=OdooDatabaseBindingRole.DESTINATION,
        scope=None,
        generation=None,
    ):
        scope = scope or self.scope
        generation = generation or self.repository.current(organization, scope)
        return OdooDatabaseBinding(
            (workspace or self.workspace).workspace_id,
            (project or self.project).project_id,
            organization,
            role,
            scope,
            generation.generation_hash,
            LOCAL_ACTOR.identity,
            self.now,
            str(uuid4()),
        )

    def declare(self, current, **changes):
        declaration = OdooDatabaseGeneration(
            current.organization_id,
            current.scope_hash,
            current.generation + 1,
            current.generation_hash,
            str(uuid4()),
            LOCAL_ACTOR.identity,
            self.now,
            "Recreated test database",
        )
        return self.repository.declare(
            replace(declaration, **changes),
            expected_generation_hash=current.generation_hash,
        )

    def test_current_page_read_does_not_write_or_create_authority(self):
        first = self.repository.current("organization-a", self.scope)
        self.assertEqual(first.generation, 0)
        self.assertEqual(first, self.repository.current("organization-a", self.scope))
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM odoo_database_generation"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM odoo_workspace_database_binding"
                ).fetchone()[0],
                0,
            )
        with self.assertRaises(OdooDatabaseBindingError):
            self.repository.assert_current(
                self.workspace.workspace_id,
                OdooDatabaseBindingRole.DESTINATION,
                self.scope,
                project_id=self.project.project_id,
            )

    def test_reset_fences_every_project_role_and_company_in_the_organization(self):
        second_project, second_workspace = self.create_workspace()
        first = self.binding()
        second = self.binding(
            second_workspace, second_project, role=OdooDatabaseBindingRole.SOURCE
        )
        self.repository.bind(first, expected_generation_hash=first.generation_hash)
        self.repository.bind(second, expected_generation_hash=second.generation_hash)
        baseline = self.repository.current("organization-a", self.scope)
        replacement = self.declare(baseline)
        self.assertEqual(replacement.generation, 1)
        for proof in (first, second):
            with self.assertRaises(OdooDatabaseGenerationConflict):
                self.repository.assert_current(
                    proof.workspace_id,
                    proof.role,
                    proof.scope_hash,
                    project_id=proof.project_id,
                )
            self.assertEqual(
                self.repository.get(proof.workspace_id, proof.role, proof.scope_hash),
                proof,
            )
            with self.assertRaises(OdooDatabaseGenerationConflict):
                self.repository.bind(
                    replace(
                        proof,
                        generation_hash=replacement.generation_hash,
                        operation_id=str(uuid4()),
                    ),
                    expected_generation_hash=replacement.generation_hash,
                )
        self.assertEqual(
            self.repository.current("organization-b", self.scope).generation, 0
        )
        _, fresh = self.create_workspace(self.project)
        fresh_proof = self.binding(fresh, self.project)
        self.assertEqual(
            self.repository.bind(
                fresh_proof, expected_generation_hash=replacement.generation_hash
            ),
            fresh_proof,
        )

    def test_different_physical_target_is_reviewable_without_overwriting_old_proof(
        self,
    ):
        first = self.binding()
        self.repository.bind(first, expected_generation_hash=first.generation_hash)
        other_scope = database_scope_hash("https://odoo.example.test", "other-database")
        second = self.binding(scope=other_scope)
        self.repository.bind(second, expected_generation_hash=second.generation_hash)
        self.assertEqual(
            self.repository.get(first.workspace_id, first.role, first.scope_hash), first
        )
        self.assertEqual(
            self.repository.get(second.workspace_id, second.role, second.scope_hash),
            second,
        )

    def test_repeat_review_retains_original_provenance_and_rejects_another_organization(
        self,
    ):
        first = self.binding()
        self.repository.bind(first, expected_generation_hash=first.generation_hash)
        replay = replace(
            first, operation_id=str(uuid4()), bound_at=self.now.replace(microsecond=0)
        )
        self.assertEqual(
            self.repository.bind(
                replay, expected_generation_hash=first.generation_hash
            ),
            first,
        )
        foreign = self.binding(organization="organization-b")
        with self.assertRaises(OdooDatabaseGenerationConflict):
            self.repository.bind(
                foreign, expected_generation_hash=foreign.generation_hash
            )
        self.assertEqual(
            self.repository.get(first.workspace_id, first.role, first.scope_hash), first
        )

    def test_rewound_or_missing_current_pointer_cannot_revalidate_old_proof(self):
        proof = self.binding()
        self.repository.bind(proof, expected_generation_hash=proof.generation_hash)
        self.declare(self.repository.current(proof.organization_id, proof.scope_hash))
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute(
                "UPDATE odoo_database_generation_current SET generation = 0, generation_hash = ?",
                [proof.generation_hash],
            )
        with self.assertRaises(OdooDatabaseBindingError):
            self.repository.assert_current(
                proof.workspace_id,
                proof.role,
                proof.scope_hash,
                project_id=proof.project_id,
            )
        with self.assertRaises(OdooDatabaseBindingError):
            self.repository.current(proof.organization_id, proof.scope_hash)
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute("DELETE FROM odoo_database_generation_current")
        with self.assertRaises(OdooDatabaseBindingError):
            self.repository.current(proof.organization_id, proof.scope_hash)

    def test_exact_declaration_replay_is_idempotent_even_after_a_later_reset(self):
        initial = self.repository.current("organization-a", self.scope)
        first = self.declare(initial)
        second = self.declare(first)
        replay = self.repository.declare(
            replace(first, declared_at=self.now.replace(microsecond=0)),
            expected_generation_hash=initial.generation_hash,
        )
        self.assertEqual(replay, first)
        self.assertEqual(self.repository.current("organization-a", self.scope), second)
        with self.assertRaises(OdooDatabaseGenerationConflict):
            self.repository.declare(
                replace(first, reason="Different request"),
                expected_generation_hash=initial.generation_hash,
            )
        with self.database.connect(self.database.registry_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM odoo_database_generation"
                ).fetchone()[0],
                3,
            )

    def test_concurrent_declarations_cannot_both_advance_one_expected_generation(self):
        initial = self.repository.current("organization-a", self.scope)
        candidates = tuple(
            OdooDatabaseGeneration(
                initial.organization_id,
                initial.scope_hash,
                1,
                initial.generation_hash,
                str(uuid4()),
                LOCAL_ACTOR.identity,
                self.now,
                "Controlled concurrent reset",
            )
            for _ in range(2)
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.repository.declare,
                    candidate,
                    expected_generation_hash=initial.generation_hash,
                )
                for candidate in candidates
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result())
                except OdooDatabaseGenerationConflict as error:
                    outcomes.append(error)
        self.assertEqual(
            sum(isinstance(value, OdooDatabaseGeneration) for value in outcomes), 1
        )
        self.assertEqual(
            self.repository.current("organization-a", self.scope).generation, 1
        )

    def test_assert_current_has_one_owner_schema_check_and_join_not_per_record_work(
        self,
    ):
        binding = self.binding()
        self.repository.bind(binding, expected_generation_hash=binding.generation_hash)
        with (
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
            retain_databases_for_operation(),
        ):
            for _ in range(2):
                self.assertEqual(
                    self.repository.assert_current(
                        binding.workspace_id,
                        binding.role,
                        binding.scope_hash,
                        project_id=binding.project_id,
                    ),
                    binding,
                )
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 1)
        self.assertEqual(
            sum(
                "FROM odoo_workspace_database_binding b" in statement
                for statement in statements
            ),
            2,
        )
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements), 1
        )

    def test_v4_upgrade_adds_empty_authority_and_preserves_existing_projects(self):
        self._downgrade_fixture_to_v4()
        with self.database.connect(self.database.registry_path) as connection:
            migration_registry.ensure_migration_registry_schema(
                connection, self.database.registry_path
            )
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone()[0],
                5,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM migration_project").fetchone()[
                    0
                ],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM odoo_workspace_database_binding"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT migration_id FROM schema_migration WHERE to_version = 5"
                ).fetchone()[0],
                "odoo-database-generation-authority-v5",
            )

    def test_failed_v4_upgrade_rolls_back_tables_version_and_ledger(self):
        self._downgrade_fixture_to_v4()

        def fail(connection):
            migration_registry._create_odoo_database_bindings(connection)
            raise RuntimeError("Controlled schema failure")

        with (
            patch.dict(
                migration_registry.MIGRATION_REGISTRY_UPGRADES,
                {4: ForwardSchemaUpgrade("controlled-v5", fail)},
            ),
            self.database.connect(self.database.registry_path) as connection,
        ):
            with self.assertRaises(MigrationStorageCompatibilityError) as raised:
                migration_registry.ensure_migration_registry_schema(
                    connection, self.database.registry_path
                )
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone()[0],
                4,
            )
            self.assertNotIn(
                ("odoo_database_generation",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migration WHERE to_version = 5"
                ).fetchone()[0],
                0,
            )

    def _downgrade_fixture_to_v4(self):
        with self.database.connect(self.database.registry_path) as connection:
            for table in (
                "saved_odoo_source_binding",
                "odoo_workspace_database_binding",
                "odoo_database_generation_current",
                "odoo_database_generation",
            ):
                connection.execute(f'DROP TABLE "{table}"')
            connection.execute("UPDATE schema_version SET version = 4")
