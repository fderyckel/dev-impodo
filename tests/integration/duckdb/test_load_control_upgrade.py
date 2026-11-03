"""Upgrade companion controls without changing old execution evidence."""

import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError
from impodo.domain.execution.models import ExecutionRowAttempt


class LoadControlUpgradeTests(unittest.TestCase):
    def test_v4_with_or_without_event_history_upgrades_without_rewriting_evidence(self):
        schema = WorkspaceEngineSchemaMixin()
        for events_present in (False, True):
            with (
                self.subTest(events_present=events_present),
                duckdb.connect(":memory:") as connection,
            ):
                schema._initialize_workspace_database(connection)
                connection.execute("UPDATE schema_version SET version = 4")
                connection.execute(
                    "INSERT INTO execution_load_control VALUES ('run', 'saved-control')"
                )
                connection.execute(
                    "INSERT INTO execution_rejected_transport VALUES ('run', 1, 'saved-rejection')"
                )
                if events_present:
                    connection.execute(
                        "INSERT INTO execution_load_control_event VALUES ('run', 0, 'saved-event')"
                    )
                else:
                    connection.execute("DROP TABLE execution_load_control_event")
                schema._ensure_workspace_database_schema(connection)
                schema._ensure_workspace_database_schema(connection)
                self.assertEqual(
                    connection.execute("SELECT version FROM schema_version").fetchone(),
                    (6,),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT * FROM execution_load_control"
                    ).fetchall(),
                    [("run", "saved-control")],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT * FROM execution_rejected_transport"
                    ).fetchall(),
                    [("run", 1, "saved-rejection")],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT * FROM execution_load_control_event"
                    ).fetchall(),
                    [("run", 0, "saved-event")] if events_present else [],
                )
                self.assertEqual(
                    connection.execute("SELECT * FROM schema_migration").fetchall(),
                    [(4, 5, "execution-load-control-events-v5"), (5, 6, "transformation-field-counts-v6")],
                )

    def test_failed_v4_upgrade_rolls_back_the_new_table_and_version(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE execution_load_control_event")
            connection.execute("UPDATE schema_version SET version = 4")
            upgrade = WORKSPACE_ENGINE_UPGRADES[4]

            def failed(current):
                upgrade.apply(current)
                raise RuntimeError("injected after event table")

            with patch.dict(
                WORKSPACE_ENGINE_UPGRADES,
                {4: ForwardSchemaUpgrade(upgrade.migration_id, failed)},
            ):
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (4,),
            )
            self.assertNotIn(
                ("execution_load_control_event",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )

    def test_incompatible_existing_event_table_is_not_silently_repaired(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE execution_load_control_event")
            connection.execute(
                "CREATE TABLE execution_load_control_event (unrecognized VARCHAR)"
            )
            connection.execute(
                "INSERT INTO execution_load_control_event VALUES ('preserve-me')"
            )
            connection.execute("UPDATE schema_version SET version = 4")
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (4,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM execution_load_control_event"
                ).fetchall(),
                [("preserve-me",)],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )

    def test_upgrade_is_atomic_and_does_not_rewrite_old_row_json(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            for table in (
                "execution_load_control",
                "execution_load_control_event",
                "execution_rejected_transport",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_version SET version = 3")
            row = ExecutionRowAttempt(
                row_id="legacy-row",
                dataset="contacts",
                source_row=92,
                target_model="res.partner",
                operation="CREATE",
                field_names=("name",),
                proposed_external_id="impodo_test.legacy",
            )
            row_json = row.to_json()
            connection.execute(
                "INSERT INTO execution_row VALUES ('legacy-run', 0, 'legacy-row', 'contacts', 'res.partner', 'CREATE', 'PLANNED', 0, NULL, '', ?, '2026-11-02T00:00:00Z')",
                [row_json],
            )
            connection.execute(
                "INSERT INTO matching_order_protected_snapshot VALUES ('check', 'old-hash', 'old-protected-evidence')"
            )
            before = connection.execute(
                "SELECT * FROM matching_order_protected_snapshot"
            ).fetchall()
            upgrade = WORKSPACE_ENGINE_UPGRADES[3]

            def failed(current):
                upgrade.apply(current)
                raise RuntimeError("injected after companion tables")

            with patch.dict(
                WORKSPACE_ENGINE_UPGRADES,
                {3: ForwardSchemaUpgrade(upgrade.migration_id, failed)},
            ):
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (3,),
            )
            self.assertNotIn(
                ("execution_load_control",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT row_json FROM execution_row").fetchone(),
                (row_json,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM matching_order_protected_snapshot"
                ).fetchall(),
                before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM execution_load_control"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version FROM schema_migration"
                ).fetchall(),
                [(3, 4), (4, 5), (5, 6)],
            )
