"""Verify retained Odoo configuration/version registry and source lineage."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.duckdb.odoo_saved_data_repository import (
    DuckDbSavedOdooDataRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.schema.migration_registry import (
    EXPECTED_REGISTRY_COLUMNS,
    MIGRATION_REGISTRY_VERSION,
    ensure_migration_registry_schema,
)
from impodo.application.data_version.service import DataVersionService
from impodo.application.project.service import MigrationProjectService
from impodo.application.run.service import MigrationRunService
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.domain.odoo_saved_data import (
    SavedOdooCaptureOperation,
    SavedOdooDataDefinition,
    SavedOdooDataVersion,
    SavedOdooRestoreBinding,
    SavedOdooRestoreProfile,
)
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationReplayError,
    MigrationStorageCompatibilityError,
)
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from tests.support.database_probe import count_duckdb_statements

SAVED_TABLES = (
    "saved_odoo_source_binding",
    "odoo_workspace_database_binding",
    "odoo_database_generation_current",
    "odoo_database_generation",
    "saved_odoo_capture_operation",
    "saved_odoo_restore_profile",
    "saved_odoo_restore_binding",
    "saved_odoo_data_version",
    "saved_odoo_data_configuration",
    "saved_odoo_data_definition",
    "saved_odoo_data_identity",
)


class SavedOdooDataRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = MigrationFoundationDatabase(Path(self.temporary.name))
        self.foundation = MigrationFoundationRepository(self.database)
        authorization = CapabilityAuthorizationPolicy()
        self.projects = MigrationProjectService(self.foundation, authorization)
        self.data_versions = DataVersionService(self.foundation, authorization)
        self.runs = MigrationRunService(self.foundation, authorization)
        self.workspaces = MigrationWorkspaceService(self.foundation, authorization)
        self.project = self.projects.create(
            actor=LOCAL_ACTOR,
            display_name="Clean accounting setup",
            migration_purpose="Preserve fictional data across resets",
            source_system_identity="Fictional Odoo source",
        )
        self.data_version = self.data_versions.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=self._project_revision(),
            purpose="AUTHORING",
            label="Clean data",
        )
        run = self.runs.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=self._project_revision(),
            data_version_id=self.data_version.data_version_id,
            purpose="AUTHORING",
            label="Capture",
        )
        self.source = self.workspaces.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=self._project_revision(),
            data_version_id=self.data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Source configuration",
        )
        self.repository = DuckDbSavedOdooDataRepository(self.database)
        now = datetime.now(UTC)
        self.definition = SavedOdooDataDefinition(
            saved_data_id=str(uuid4()),
            project_id=self.project.project_id,
            label="Clean chart",
            revision=1,
            source_workspace_id=self.source.workspace_id,
            source_target_hash=content_hash("source-dev"),
            source_company_hash=content_hash("Company A"),
            odoo_major=20,
            config_json=canonical_json(
                {"models": ["account.account"], "all_fields": True}
            ),
            latest_version=None,
            created_at=now,
            updated_at=now,
        )

    def _project_revision(self) -> int:
        return self.projects.get(
            self.project.project_id, actor=LOCAL_ACTOR
        ).optimistic_revision

    def _accept_fixture_data(self) -> None:
        # Source-package acceptance is tested by the capture owner. This
        # persistence fixture establishes only the frozen registry prerequisite.
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute(
                "UPDATE data_version SET state = 'FROZEN', frozen_at = ? WHERE data_version_id = ?",
                [datetime.now(UTC).isoformat(), self.data_version.data_version_id],
            )

    def _version(
        self, number: int = 1, configuration_revision: int = 1
    ) -> SavedOdooDataVersion:
        return SavedOdooDataVersion(
            saved_data_id=self.definition.saved_data_id,
            project_id=self.project.project_id,
            version_number=number,
            configuration_revision=configuration_revision,
            workspace_id=self.source.workspace_id,
            data_version_id=self.data_version.data_version_id,
            source_selection_hash=content_hash(f"selection-{number}"),
            manifest_hashes=(("account.account", content_hash(f"manifest-{number}")),),
            row_counts=(("account.account", 10),),
            captured_at=datetime.now(UTC),
            operation_id=str(uuid4()),
        )

    def test_capture_configuration_can_precede_data_and_revision_history_is_immutable(
        self,
    ) -> None:
        first = self.repository.create_definition(self.definition, source_binding_hash=None)
        self.assertEqual(self.repository.create_definition(self.definition, source_binding_hash=None), first)
        self.assertEqual(
            self.repository.list_versions(first.project_id, first.saved_data_id), ()
        )
        second = self.repository.save_definition(
            replace(
                first,
                label="Clean chart and tags",
                config_json='{"models":["account.account","account.account.tag"]}',
            ),
            expected_revision=first.revision,
            source_binding_hash=None,
        )
        self.assertEqual((second.revision, second.configuration_revision), (2, 2))
        self.assertEqual(
            self.repository.get_configuration(first.project_id, first.saved_data_id, 1),
            first,
        )
        self.assertEqual(
            self.repository.get_configuration(first.project_id, first.saved_data_id, 2),
            second,
        )
        self.assertEqual(
            self.repository.get_definition(first.project_id, first.saved_data_id),
            second,
        )
        with self.assertRaises(MigrationConflictError):
            self.repository.save_definition(
                replace(first, label="Stale choice"), expected_revision=1,
                source_binding_hash=None,
            )

    def test_each_supported_major_round_trips_without_a_default_alias(self) -> None:
        for major in (19, 20):
            with self.subTest(major=major):
                definition = replace(
                    self.definition, saved_data_id=str(uuid4()), odoo_major=major
                )
                self.repository.create_definition(definition, source_binding_hash=None)
                self.assertEqual(
                    self.repository.get_definition(
                        definition.project_id, definition.saved_data_id
                    ).odoo_major,
                    major,
                )

    def test_versions_publish_atomically_and_replay_without_duplicates_after_reopen(
        self,
    ) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        self._accept_fixture_data()
        version = self._version()
        updated, saved = self.repository.append_version(version, expected_revision=1, source_binding_hash=None)
        self.assertEqual(
            (updated.revision, updated.configuration_revision, updated.latest_version),
            (2, 1, 1),
        )
        self.assertEqual(saved, version)
        self.assertEqual(self.repository.create_definition(self.definition, source_binding_hash=None), updated)
        with self.assertRaises(MigrationOperationReplayError):
            self.repository.create_definition(replace(self.definition, label="Different creation meaning"), source_binding_hash=None)
        reopened = DuckDbSavedOdooDataRepository(
            MigrationFoundationDatabase(self.database.root)
        )
        self.assertEqual(
            reopened.append_version(version, expected_revision=1, source_binding_hash=None), (updated, version)
        )
        self.assertEqual(
            reopened.list_versions(updated.project_id, updated.saved_data_id),
            (version,),
        )
        self.assertTrue(reopened.retained_data_version(version.data_version_id))
        self.assertEqual(
            reopened.get_version_for_operation(
                version.project_id, version.saved_data_id, version.operation_id
            ),
            version,
        )
        self.assertIsNone(
            reopened.get_version_for_operation(
                version.project_id, version.saved_data_id, str(uuid4())
            )
        )
        with self.assertRaises(MigrationOperationReplayError):
            reopened.append_version(
                replace(version, row_counts=(("account.account", 11),)),
                expected_revision=updated.revision,
                source_binding_hash=None,
            )
        self.assertEqual(
            reopened.get_definition(
                updated.project_id, updated.saved_data_id
            ).latest_version,
            1,
        )

    def test_capture_operation_reopens_exact_workspace_before_capture_acceptance(self) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        operation = SavedOdooCaptureOperation(
            operation_id=str(uuid4()), project_id=self.definition.project_id,
            saved_data_id=self.definition.saved_data_id, expected_revision=1,
            configuration_revision=1, workspace_id=self.source.workspace_id,
            data_version_id=self.data_version.data_version_id, created_at=datetime.now(UTC),
        )
        self.assertIsNone(self.repository.get_capture_operation(operation.project_id, operation.saved_data_id, operation.operation_id))
        self.assertEqual(self.repository.bind_capture_operation(operation), operation)
        reopened = DuckDbSavedOdooDataRepository(MigrationFoundationDatabase(self.database.root))
        self.assertEqual(reopened.get_capture_operation(operation.project_id, operation.saved_data_id, operation.operation_id), operation)
        self.assertEqual(reopened.bind_capture_operation(operation), operation)
        self.assertTrue(reopened.retained_data_version(operation.data_version_id))
        with self.assertRaises(MigrationOperationReplayError):
            reopened.bind_capture_operation(replace(operation, workspace_id=str(uuid4())))

    def test_stale_and_incomplete_capture_cannot_replace_last_complete_version(
        self,
    ) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        with self.assertRaises(MigrationFoundationError):
            self.repository.append_version(self._version(), expected_revision=1, source_binding_hash=None)
        self.assertIsNone(
            self.repository.get_definition(
                self.definition.project_id, self.definition.saved_data_id
            ).latest_version
        )
        self._accept_fixture_data()
        updated, first = self.repository.append_version(
            self._version(), expected_revision=1,
            source_binding_hash=None,
        )
        changed = self.repository.save_definition(
            replace(updated, label="Changed capture scope"),
            expected_revision=updated.revision,
            source_binding_hash=None,
        )
        with self.assertRaises(MigrationConflictError):
            self.repository.append_version(
                self._version(2), expected_revision=changed.revision,
                source_binding_hash=None,
            )
        self.assertEqual(
            self.repository.get_version(first.project_id, first.saved_data_id, 1), first
        )
        self.assertEqual(
            self.repository.get_definition(
                first.project_id, first.saved_data_id
            ).latest_version,
            1,
        )

    def test_cross_project_and_source_changes_are_rejected(self) -> None:
        definition = self.repository.create_definition(self.definition, source_binding_hash=None)
        wrong_project = str(uuid4())
        with self.assertRaises(MigrationNotFoundError):
            self.repository.get_definition(wrong_project, definition.saved_data_id)
        with self.assertRaises(MigrationFoundationError):
            self.repository.save_definition(
                replace(definition, source_target_hash=content_hash("other-database")),
                expected_revision=1,
                source_binding_hash=None,
            )
        self._accept_fixture_data()
        with self.assertRaises(MigrationNotFoundError):
            self.repository.append_version(
                replace(self._version(), project_id=wrong_project), expected_revision=1,
                source_binding_hash=None,
            )

    def test_restore_binding_preserves_source_and_requires_fresh_matching_lineage(
        self,
    ) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        self._accept_fixture_data()
        _updated, version = self.repository.append_version(
            self._version(), expected_revision=1,
            source_binding_hash=None,
        )
        run = self.runs.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=self._project_revision(),
            data_version_id=version.data_version_id,
            purpose="AUTHORING",
            label="Restore",
        )
        workspace = self.workspaces.create(
            self.project.project_id,
            actor=LOCAL_ACTOR,
            expected_workspace_revision=self._project_revision(),
            data_version_id=version.data_version_id,
            migration_run_id=run.migration_run_id,
            display_name="Fresh destination restore",
        )
        binding = SavedOdooRestoreBinding(
            project_id=self.project.project_id,
            workspace_id=workspace.workspace_id,
            saved_data_id=version.saved_data_id,
            version_number=1,
            source_workspace_id=version.workspace_id,
            data_version_id=version.data_version_id,
            generation_id=str(uuid4()),
            created_at=datetime.now(UTC),
        )
        self.assertIsNone(self.repository.get_restore_binding(workspace.workspace_id))
        self.assertEqual(self.repository.bind_restore(binding), binding)
        self.assertEqual(self.repository.bind_restore(binding), binding)
        self.assertEqual(
            self.repository.get_restore_binding(workspace.workspace_id), binding
        )
        self.assertEqual(self.repository.list_restore_bindings(binding.project_id, binding.saved_data_id), (binding,))
        self.assertEqual(self.repository.list_restore_bindings(binding.project_id, binding.saved_data_id, offset=1), ())
        self.assertEqual(self.repository.list_project_restore_bindings(binding.project_id), (binding,))
        self.assertEqual(self.repository.list_project_restore_bindings(binding.project_id, offset=1), ())
        self.assertEqual(self.repository.list_project_restore_bindings(str(uuid4())), ())
        with self.assertRaises(MigrationOperationReplayError):
            self.repository.bind_restore(replace(binding, generation_id=str(uuid4())))

    def test_corrupt_saved_content_is_rejected_instead_of_reused(self) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        with self.database.connect(self.database.registry_path) as connection:
            connection.execute(
                "UPDATE saved_odoo_data_definition SET content_hash = ?",
                [content_hash("tampered")],
            )
        with self.assertRaises(MigrationStorageCompatibilityError):
            self.repository.get_definition(
                self.definition.project_id, self.definition.saved_data_id
            )

    def test_configuration_rejects_credentials_and_lists_have_explicit_bounds(
        self,
    ) -> None:
        with self.assertRaises(MigrationFoundationError):
            replace(self.definition, config_json='{"nested":{"api_key":"secret"}}')
        with self.assertRaises(MigrationFoundationError):
            self.repository.list_definitions(self.definition.project_id, limit=501)
        self.repository.create_definition(self.definition, source_binding_hash=None)
        self.assertEqual(
            self.repository.list_definitions(self.definition.project_id, limit=1),
            (self.definition,),
        )
        self.assertEqual(
            self.repository.list_definitions(
                self.definition.project_id, limit=1, offset=1
            ),
            (),
        )

    def test_configuration_page_uses_one_registry_owner_and_bounded_sql(self) -> None:
        for index in range(3):
            self.repository.create_definition(
                replace(
                    self.definition,
                    saved_data_id=str(uuid4()),
                    label=f"Saved set {index}",
                ),
                source_binding_hash=None,
            )
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            records = self.repository.list_definitions(
                self.definition.project_id, limit=3
            )
        self.assertEqual(len(records), 3)
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 1)
        # One current-schema check uses four statements. Listing performs
        # one bulk SELECT regardless of the number of saved configurations.
        self.assertLessEqual(len(statements), 5)

    def test_retained_versions_block_project_deletion_before_artifacts_move(self) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        self._accept_fixture_data()
        _definition, version = self.repository.append_version(self._version(), expected_revision=1, source_binding_hash=None)
        source_directory = self.database.workspace_directory(self.project.project_id, self.source.workspace_id)
        self.assertTrue(source_directory.is_dir())

        with patch.object(self.foundation, "_stage_project_directories", side_effect=AssertionError("Retained source must not be staged")):
            with self.assertRaisesRegex(MigrationConflictError, "saved Odoo data for future restores"):
                self.projects.delete(
                    self.project.project_id, actor=LOCAL_ACTOR,
                    expected_revision=self._project_revision(),
                )

        self.assertTrue(source_directory.is_dir())
        self.assertEqual(self.repository.get_version(version.project_id, version.saved_data_id, 1), version)
        self.assertIsNotNone(self.projects.get(self.project.project_id, actor=LOCAL_ACTOR))

    def test_empty_saved_configuration_does_not_block_normal_project_deletion(self) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        self.projects.delete(
            self.project.project_id, actor=LOCAL_ACTOR,
            expected_revision=self._project_revision(),
        )
        with self.assertRaises(MigrationNotFoundError):
            self.projects.get(self.project.project_id, actor=LOCAL_ACTOR)

    def test_restore_profiles_remember_portable_choices_with_cas_and_project_scope(
        self,
    ) -> None:
        self.repository.create_definition(self.definition, source_binding_hash=None)
        now = datetime.now(UTC)
        profile = SavedOdooRestoreProfile(
            profile_id=str(uuid4()),
            project_id=self.project.project_id,
            saved_data_id=self.definition.saved_data_id,
            label="Test database",
            revision=1,
            connection_mode="REMOTE",
            base_url="https://odoo.example.test",
            database="odoo-test",
            company_name="Company A",
            credential_workspace_id=self.source.workspace_id,
            settings_json='{"field_policies":{"account.account.name":"USE_SAVED"}}',
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(
            self.repository.save_restore_profile(profile, expected_revision=None),
            profile,
        )
        self.assertEqual(
            self.repository.save_restore_profile(profile, expected_revision=None),
            profile,
        )
        self.assertEqual(
            self.repository.list_restore_profiles(
                profile.project_id, profile.saved_data_id
            ),
            (profile,),
        )
        changed = replace(
            profile,
            settings_json='{"field_policies":{"account.account.name":"KEEP_DESTINATION"}}',
        )
        updated = self.repository.save_restore_profile(changed, expected_revision=1)
        self.assertEqual(updated.revision, 2)
        reopened = DuckDbSavedOdooDataRepository(
            MigrationFoundationDatabase(self.database.root)
        )
        self.assertEqual(
            reopened.get_restore_profile(
                profile.project_id, profile.saved_data_id, profile.profile_id
            ),
            updated,
        )
        with self.assertRaises(MigrationConflictError):
            reopened.save_restore_profile(changed, expected_revision=1)
        with self.assertRaises(MigrationNotFoundError):
            reopened.get_restore_profile(
                str(uuid4()), profile.saved_data_id, profile.profile_id
            )
        with self.assertRaises(MigrationFoundationError):
            replace(profile, settings_json='{"api_key":"must not be stored"}')


class SavedOdooRegistryUpgradeTests(unittest.TestCase):
    def test_recognized_registry_v1_upgrades_without_changing_existing_data(
        self,
    ) -> None:
        with duckdb.connect(":memory:") as connection:
            ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            for table in SAVED_TABLES:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_version SET version = 1")
            ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (MIGRATION_REGISTRY_VERSION,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version, migration_id FROM schema_migration"
                ).fetchall(),
                [
                    (1, 2, "saved-odoo-data-and-restore-v2"),
                    (2, 3, "saved-odoo-restore-profile-v3"),
                    (3, 4, "saved-odoo-capture-recovery-v4"),
                    (4, 5, "odoo-database-generation-authority-v5"),
                ],
            )
            self.assertEqual(
                {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()},
                set(EXPECTED_REGISTRY_COLUMNS),
            )

    def test_corrupt_baseline_rolls_back_added_tables_and_version(self) -> None:
        with duckdb.connect(":memory:") as connection:
            ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            for table in SAVED_TABLES:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_version SET version = 1")
            connection.execute(
                "ALTER TABLE production_run_values ADD COLUMN unexpected VARCHAR"
            )
            with self.assertRaises(MigrationStorageCompatibilityError):
                ensure_migration_registry_schema(connection, Path("registry.duckdb"))
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (1,),
            )
            self.assertFalse(
                set(SAVED_TABLES)
                & {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )


if __name__ == "__main__":
    unittest.main()
