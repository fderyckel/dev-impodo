"""Upgrade private companions without rewriting existing execution evidence."""

import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class OperatorErrorUpgradeTests(unittest.TestCase):
    def test_v7_upgrade_rolls_back_and_reopens_without_rewriting_evidence(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE mapping_validation_current")
            connection.execute("DROP TABLE execution_operator_error")
            connection.execute("UPDATE schema_version SET version = 7")
            connection.execute(
                "INSERT INTO matching_order_protected_snapshot VALUES ('check', 'hash', 'immutable-evidence')"
            )
            before = connection.execute(
                "SELECT * FROM matching_order_protected_snapshot"
            ).fetchall()
            upgrade = WORKSPACE_ENGINE_UPGRADES[7]

            def interrupted(current):
                upgrade.apply(current)
                raise RuntimeError("injected interruption")

            with (
                patch.dict(
                    WORKSPACE_ENGINE_UPGRADES,
                    {7: ForwardSchemaUpgrade(upgrade.migration_id, interrupted)},
                ),
                self.assertRaises(WorkspaceStateCompatibilityError),
            ):
                schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (7,),
            )
            self.assertNotIn(
                ("execution_operator_error",),
                connection.execute("SHOW TABLES").fetchall(),
            )
            schema._ensure_workspace_database_schema(connection)
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM matching_order_protected_snapshot"
                ).fetchall(),
                before,
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(),
                [(7, 8, "private-operator-errors-v8"), (8, 9, "current-mapping-validation-v9")],
            )

    def test_incompatible_private_table_is_preserved_and_upgrade_rejected(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE mapping_validation_current")
            connection.execute("DROP TABLE execution_operator_error")
            connection.execute(
                "CREATE TABLE execution_operator_error (unrecognized VARCHAR)"
            )
            connection.execute(
                "INSERT INTO execution_operator_error VALUES ('preserve-me')"
            )
            connection.execute("UPDATE schema_version SET version = 7")
            with self.assertRaises(WorkspaceStateCompatibilityError):
                schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT * FROM execution_operator_error").fetchall(),
                [("preserve-me",)],
            )
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (7,),
            )
