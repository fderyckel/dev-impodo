"""Verify one-way, fail-closed upgrades for every current DuckDB store."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import duckdb

from impodo.adapters.duckdb.constants import (
    SCHEMA_BASELINE_VERSION,
    SCHEMA_GENERATION,
    SCHEMA_VERSION,
)
from impodo.adapters.duckdb.schema.data_version_store import (
    DATA_VERSION_STORE_BASELINE_VERSION,
    DATA_VERSION_STORE_GENERATION,
    DATA_VERSION_STORE_UPGRADES,
    DATA_VERSION_STORE_VERSION,
    ensure_data_version_store,
    initialize_data_version_store,
)
from impodo.adapters.duckdb.schema.forward_upgrades import (
    ForwardSchemaUpgrade,
    create_schema_migration_ledger,
    ensure_current_schema,
)
from impodo.adapters.duckdb.schema.migration_registry import (
    MIGRATION_REGISTRY_BASELINE_VERSION,
    MIGRATION_REGISTRY_GENERATION,
    MIGRATION_REGISTRY_UPGRADES,
    MIGRATION_REGISTRY_VERSION,
    ensure_migration_registry_schema,
)
from impodo.adapters.duckdb.schema.migration_workspace_store import (
    MIGRATION_WORKSPACE_BASELINE_VERSION,
    MIGRATION_WORKSPACE_GENERATION,
    MIGRATION_WORKSPACE_UPGRADES,
    MIGRATION_WORKSPACE_VERSION,
    ensure_migration_workspace_store,
    initialize_migration_workspace_store,
)
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.data_version.models import DataVersion
from impodo.domain.project.foundation import MigrationStorageCompatibilityError
from impodo.domain.workspace.models import MigrationWorkspace
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError

PROJECT_ID = "10000000-0000-4000-8000-000000000001"
DATA_VERSION_ID = "20000000-0000-4000-8000-000000000001"
RUN_ID = "30000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "40000000-0000-4000-8000-000000000001"
CREATED_AT = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def _data_version() -> DataVersion:
    return DataVersion(
        data_version_id=DATA_VERSION_ID,
        project_id=PROJECT_ID,
        version_number=1,
        parent_data_version_id=None,
        purpose="AUTHORING",
        state="DRAFT",
        label="Representative export",
        export_as_of="",
        source_package_hash=None,
        optimistic_revision=1,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _workspace() -> MigrationWorkspace:
    return MigrationWorkspace(
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        data_version_id=DATA_VERSION_ID,
        migration_run_id=RUN_ID,
        recipe_application_id=None,
        display_name="Customer authoring",
        state="OPEN",
        optimistic_revision=1,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _schema_fingerprint(connection: duckdb.DuckDBPyConnection) -> str:
    shape = []
    for row in connection.execute("SHOW TABLES").fetchall():
        table = str(row[0])
        columns = tuple(
            str(column[1])
            for column in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
        )
        shape.append((table, columns))
    shape.sort()
    encoded = json.dumps(shape, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class ForwardUpgradeCompatibilityTests(unittest.TestCase):
    def test_every_current_store_has_a_contiguous_upgrade_path(self) -> None:
        stores = (
            (
                MIGRATION_REGISTRY_BASELINE_VERSION,
                MIGRATION_REGISTRY_VERSION,
                MIGRATION_REGISTRY_UPGRADES,
            ),
            (
                DATA_VERSION_STORE_BASELINE_VERSION,
                DATA_VERSION_STORE_VERSION,
                DATA_VERSION_STORE_UPGRADES,
            ),
            (
                MIGRATION_WORKSPACE_BASELINE_VERSION,
                MIGRATION_WORKSPACE_VERSION,
                MIGRATION_WORKSPACE_UPGRADES,
            ),
            (
                SCHEMA_BASELINE_VERSION,
                SCHEMA_VERSION,
                WORKSPACE_ENGINE_UPGRADES,
            ),
        )
        for baseline, current, upgrades in stores:
            with self.subTest(current=current, upgrades=upgrades):
                self.assertEqual(
                    tuple(sorted(upgrades)),
                    tuple(range(baseline, current)),
                )

    def test_fresh_stores_start_current_with_empty_upgrade_ledgers(self) -> None:
        data_version = _data_version()
        workspace = _workspace()
        schema = WorkspaceEngineSchemaMixin()
        stores = (
            (
                lambda c: ensure_migration_registry_schema(c, Path("registry.duckdb")),
                lambda c: ensure_migration_registry_schema(c, Path("registry.duckdb")),
                MIGRATION_REGISTRY_GENERATION,
                MIGRATION_REGISTRY_VERSION,
                "e546d0a428ea0eb285bfbcb830f1dc02a44fa2ac15989b56fdd4e37c754d2bac",
            ),
            (
                lambda c: initialize_data_version_store(c, data_version),
                lambda c: ensure_data_version_store(
                    c, Path("data-version.duckdb"), data_version
                ),
                DATA_VERSION_STORE_GENERATION,
                DATA_VERSION_STORE_VERSION,
                "ffa11ff6724b012deda8d389d179cbe0c28541f5f60d42bc6975c49dad8dab01",
            ),
            (
                lambda c: initialize_migration_workspace_store(c, workspace),
                lambda c: ensure_migration_workspace_store(
                    c, Path("workspace.duckdb"), workspace
                ),
                MIGRATION_WORKSPACE_GENERATION,
                MIGRATION_WORKSPACE_VERSION,
                "20fcb20795a4a27fe668002bc3b9ad8b8547a0cfd4fc9c680b04ca1df0f092f1",
            ),
            (
                schema._initialize_workspace_database,
                schema._ensure_workspace_database_schema,
                SCHEMA_GENERATION,
                SCHEMA_VERSION,
                "102696ef8dc8a045ee05bfac70dc05d730f69603bfaefd90c361bdfce4c4ca9e",
            ),
        )
        for initialize, ensure, generation, version, fingerprint in stores:
            with (
                self.subTest(generation=generation),
                duckdb.connect(":memory:") as connection,
            ):
                initialize(connection)
                ensure(connection)
                ensure(connection)
                self.assertEqual(version, 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT generation, version FROM schema_version"
                    ).fetchone(),
                    (generation, version),
                )
                self.assertEqual(_schema_fingerprint(connection), fingerprint)
                self.assertEqual(
                    connection.execute("SELECT * FROM schema_migration").fetchall(), []
                )
                for bad_generation, bad_version in (
                    (generation, 0),
                    (generation, version + 1),
                    ("unsupported-generation", version),
                ):
                    with self.subTest(stored=(bad_generation, bad_version)):
                        connection.execute(
                            "UPDATE schema_version SET generation = ?, version = ?",
                            [bad_generation, bad_version],
                        )
                        with self.assertRaises(
                            (
                                MigrationStorageCompatibilityError,
                                WorkspaceStateCompatibilityError,
                            )
                        ):
                            ensure(connection)
                        self.assertEqual(
                            connection.execute(
                                "SELECT generation, version FROM schema_version"
                            ).fetchone(),
                            (bad_generation, bad_version),
                        )
                        self.assertEqual(_schema_fingerprint(connection), fingerprint)
                        self.assertEqual(
                            connection.execute(
                                "SELECT * FROM schema_migration"
                            ).fetchall(),
                            [],
                        )

    def test_failed_upgrade_rolls_back_ddl_version_and_ledger(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE schema_version (
                    singleton_id INTEGER PRIMARY KEY,
                    generation VARCHAR NOT NULL,
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_version VALUES (1, 'test-generation', 1);
                CREATE TABLE payload (payload_id INTEGER PRIMARY KEY);
                INSERT INTO payload VALUES (7);
                """
            )

            def add_v2(
                current: duckdb.DuckDBPyConnection,
            ) -> None:
                create_schema_migration_ledger(current)
                current.execute("ALTER TABLE payload ADD COLUMN v2_value VARCHAR")

            def fail_while_adding_v3(
                current: duckdb.DuckDBPyConnection,
            ) -> None:
                current.execute("ALTER TABLE payload ADD COLUMN v3_value VARCHAR")
                raise RuntimeError("simulated interruption")

            with self.assertRaisesRegex(RuntimeError, "incompatible test store"):
                ensure_current_schema(
                    connection,
                    expected_generation="test-generation",
                    baseline_version=1,
                    target_version=3,
                    upgrades={
                        1: ForwardSchemaUpgrade(
                            migration_id="test-v1-to-v2",
                            apply=add_v2,
                        ),
                        2: ForwardSchemaUpgrade(
                            migration_id="test-v2-to-v3",
                            apply=fail_while_adding_v3,
                        ),
                    },
                    validate_current=lambda: None,
                    compatibility_error=lambda: RuntimeError("incompatible test store"),
                )

            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SHOW TABLES").fetchall(),
                [("payload",), ("schema_version",)],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM payload").fetchall(),
                [(7,)],
            )
        finally:
            connection.close()

    def test_multiple_upgrade_steps_apply_in_order_in_one_open(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE schema_version (
                    singleton_id INTEGER PRIMARY KEY,
                    generation VARCHAR NOT NULL,
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_version VALUES (1, 'test-generation', 1);
                """
            )

            ensure_current_schema(
                connection,
                expected_generation="test-generation",
                baseline_version=1,
                target_version=3,
                upgrades={
                    1: ForwardSchemaUpgrade(
                        migration_id="test-v1-to-v2",
                        apply=create_schema_migration_ledger,
                    ),
                    2: ForwardSchemaUpgrade(
                        migration_id="test-v2-to-v3",
                        apply=lambda current: current.execute(
                            "CREATE TABLE current_shape (singleton_id INTEGER)"
                        ),
                    ),
                },
                validate_current=lambda: connection.execute(
                    "SELECT singleton_id FROM current_shape"
                ).fetchall(),
                compatibility_error=lambda: RuntimeError("incompatible test store"),
            )

            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (3,),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT from_version, to_version, migration_id
                      FROM schema_migration
                     ORDER BY to_version
                    """
                ).fetchall(),
                [
                    (1, 2, "test-v1-to-v2"),
                    (2, 3, "test-v2-to-v3"),
                ],
            )
        finally:
            connection.close()

    def test_newer_workspace_version_is_rejected_without_mutation(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)
            connection.execute(
                "UPDATE schema_version SET version = ?",
                [SCHEMA_VERSION + 1],
            )

            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)

            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (SCHEMA_VERSION + 1,),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(),
                [],
            )
        finally:
            connection.close()

    def test_missing_upgrade_step_is_rejected_before_any_write(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE schema_version (
                    singleton_id INTEGER PRIMARY KEY,
                    generation VARCHAR NOT NULL,
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_version VALUES (1, 'test-generation', 1);
                """
            )

            with self.assertRaisesRegex(RuntimeError, "missing upgrade"):
                ensure_current_schema(
                    connection,
                    expected_generation="test-generation",
                    baseline_version=1,
                    target_version=2,
                    upgrades={},
                    validate_current=lambda: None,
                    compatibility_error=lambda: RuntimeError("missing upgrade"),
                )

            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SHOW TABLES").fetchall(),
                [("schema_version",)],
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
