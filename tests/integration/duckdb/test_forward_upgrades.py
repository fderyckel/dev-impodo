"""Verify one-way, fail-closed upgrades for every current DuckDB store."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import unittest

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


def _restore_v1_shape(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("DROP TABLE IF EXISTS correction_run_binding")
    connection.execute("DROP TABLE IF EXISTS mapping_mutation_receipt")
    connection.execute("DROP TABLE IF EXISTS test_run_parameter_values")
    connection.execute("DROP TABLE IF EXISTS test_run_setup_binding")
    connection.execute("DROP TABLE schema_migration")
    tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
    if "workspace_projection_cache" in tables:
        for column in (
            "destination_odoo_connection_mode",
            "destination_odoo_base_url",
            "destination_odoo_database",
            "destination_verified_target_hash",
            "destination_verified_credential_binding_hash",
            "destination_verified_read_principal_hash",
            "destination_verified_odoo_version",
            "destination_verified_at",
            "destination_match_plan_json",
            "transfer_order_plan_json",
        ):
            connection.execute(
                f"ALTER TABLE workspace_projection_cache DROP COLUMN {column}"
            )
    connection.execute("UPDATE schema_version SET version = 1")


def _schema_fingerprint(connection: duckdb.DuckDBPyConnection) -> str:
    shape = []
    for row in connection.execute("SHOW TABLES").fetchall():
        table = str(row[0])
        columns = tuple(
            str(column[1])
            for column in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        shape.append((table, columns))
    shape.sort()
    encoded = json.dumps(shape, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _assert_upgrade_record(
    test: unittest.TestCase,
    connection: duckdb.DuckDBPyConnection,
    *,
    generation: str,
    version: int,
    migration_id: str,
) -> None:
    test.assertEqual(
        connection.execute(
            "SELECT generation, version FROM schema_version"
        ).fetchone(),
        (generation, version),
    )
    test.assertEqual(
        connection.execute(
            """
            SELECT from_version, to_version, migration_id
              FROM schema_migration
            """
        ).fetchall(),
        [(1, 2, migration_id)],
    )


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

    def test_registry_v1_upgrades_once_and_keeps_its_exact_baseline(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            path = Path("C:/impodo/registry.duckdb")
            ensure_migration_registry_schema(connection, path)
            connection.execute(
                "INSERT INTO migration_project_identity VALUES (?)",
                [PROJECT_ID],
            )
            connection.execute(
                """
                INSERT INTO migration_project VALUES (
                    ?, 'Legacy ERP rollout', 'Move customers to Odoo 19',
                    'Legacy ERP', 'BUSINESS', 365, 'ACTIVE', 4, ?, ?, NULL, NULL
                )
                """,
                [PROJECT_ID, CREATED_AT.isoformat(), CREATED_AT.isoformat()],
            )
            _restore_v1_shape(connection)
            self.assertEqual(
                _schema_fingerprint(connection),
                "c0c35e7134c511ccd3aa3102d90e4f0184d871e8f1761ca2df5cca338263e590",
            )

            ensure_migration_registry_schema(connection, path)
            ensure_migration_registry_schema(connection, path)

            self.assertEqual(
                connection.execute(
                    "SELECT generation, version FROM schema_version"
                ).fetchone(),
                (MIGRATION_REGISTRY_GENERATION, MIGRATION_REGISTRY_VERSION),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version, migration_id "
                    "FROM schema_migration ORDER BY from_version"
                ).fetchall(),
                [
                    (1, 2, "migration-registry-v1-to-v2-migration-ledger"),
                    (2, 3, "migration-registry-v2-to-v3-test-run-setup"),
                    (3, 4, "migration-registry-v3-to-v4-test-run-values"),
                    (4, 5, "migration-registry-v4-to-v5-correction-binding"),
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT display_name, status FROM migration_project"
                ).fetchone(),
                ("Legacy ERP rollout", "ACTIVE"),
            )
        finally:
            connection.close()

    def test_data_version_v1_upgrades_without_rewriting_package_data(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            data_version = _data_version()
            path = Path(
                "C:/impodo/projects/project/data-versions/version/data-version.duckdb"
            )
            initialize_data_version_store(connection, data_version)
            _restore_v1_shape(connection)
            self.assertEqual(
                _schema_fingerprint(connection),
                "0d7258a38c15d151cba868144570851d06155bc502779f0014a0efc36a8b61f9",
            )

            ensure_data_version_store(connection, path, data_version)

            _assert_upgrade_record(
                self,
                connection,
                generation=DATA_VERSION_STORE_GENERATION,
                version=DATA_VERSION_STORE_VERSION,
                migration_id="data-version-store-v1-to-v2-migration-ledger",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT data_version_id FROM data_version_identity"
                ).fetchone(),
                (DATA_VERSION_ID,),
            )
        finally:
            connection.close()

    def test_workspace_reference_v1_upgrades_without_copying_source_data(self) -> None:
        connection = duckdb.connect(":memory:")
        try:
            workspace = _workspace()
            path = Path(
                "C:/impodo/projects/project/workspaces/workspace/workspace.duckdb"
            )
            initialize_migration_workspace_store(connection, workspace)
            _restore_v1_shape(connection)
            self.assertEqual(
                _schema_fingerprint(connection),
                "d59e093e308676b8407a421b411270eb451bce8b0481a3e88f0ecda6e216c310",
            )

            ensure_migration_workspace_store(connection, path, workspace)

            _assert_upgrade_record(
                self,
                connection,
                generation=MIGRATION_WORKSPACE_GENERATION,
                version=MIGRATION_WORKSPACE_VERSION,
                migration_id="migration-workspace-v1-to-v2-migration-ledger",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT workspace_id, data_version_id FROM workspace_linkage"
                ).fetchone(),
                (WORKSPACE_ID, DATA_VERSION_ID),
            )
        finally:
            connection.close()

    def test_workspace_engine_v1_upgrades_once_without_row_processing(self) -> None:
        schema = WorkspaceEngineSchemaMixin()
        connection = duckdb.connect(":memory:")
        try:
            schema._initialize_workspace_database(connection)
            _restore_v1_shape(connection)
            self.assertEqual(
                _schema_fingerprint(connection),
                "c6b9e0481b2d0e8126515a6900062a71b634d19808e17e744b9746b98c2ff532",
            )

            schema._ensure_workspace_database_schema(connection)
            schema._ensure_workspace_database_schema(connection)

            self.assertEqual(
                connection.execute(
                    "SELECT generation, version FROM schema_version"
                ).fetchone(),
                (SCHEMA_GENERATION, SCHEMA_VERSION),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version, migration_id "
                    "FROM schema_migration ORDER BY from_version"
                ).fetchall(),
                [
                    (1, 2, "workspace-engine-v1-to-v2-migration-ledger"),
                    (
                        2,
                        3,
                        "workspace-engine-v2-to-v3-mapping-mutation-receipts",
                    ),
                    (
                        3,
                        4,
                        "workspace-engine-v3-to-v4-odoo-capture-sets",
                    ),
                    (
                        4,
                        5,
                        "workspace-engine-v4-to-v5-transfer-destination",
                    ),
                    (
                        5,
                        6,
                        "workspace-engine-v5-to-v6-destination-matching",
                    ),
                    (
                        6,
                        7,
                        "workspace-engine-v6-to-v7-transfer-order",
                    ),
                ],
            )
            self.assertEqual(
                tuple(
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('odoo_capture_selection_current')"
                    ).fetchall()
                ),
                ("model", "selection_id", "version"),
            )
            self.assertEqual(
                tuple(
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('odoo_capture_manifest_current')"
                    ).fetchall()
                ),
                ("dataset_id", "manifest_id"),
            )
        finally:
            connection.close()

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
                    compatibility_error=lambda: RuntimeError(
                        "incompatible test store"
                    ),
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
