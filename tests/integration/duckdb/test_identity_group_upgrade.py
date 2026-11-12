"""Upgrade the first-project schema without rewriting protected check evidence."""

import unittest
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.domain.workspace.workbench import WorkspaceStateCompatibilityError


class IdentityGroupUpgradeTests(unittest.TestCase):
    def test_version_one_upgrade_preserves_evidence_and_rolls_back_a_failed_step(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("DROP TABLE odoo_saved_source_company")
            connection.execute("DROP TABLE execution_load_control")
            connection.execute("DROP TABLE execution_rejected_transport")
            connection.execute("DROP TABLE execution_load_control_event")
            connection.execute("DROP TABLE matching_order_protected_relationship_choice")
            connection.execute(
                "DROP INDEX matching_order_protected_identity_group_lookup"
            )
            connection.execute(
                "DROP INDEX matching_order_protected_identity_choice_lookup"
            )
            for column in (
                "group_hash",
                "member_origin",
                "member_position",
                "source_match_count",
            ):
                connection.execute(
                    f"ALTER TABLE matching_order_protected_identity_choice DROP COLUMN {column}"
                )
            connection.execute(
                "CREATE INDEX matching_order_protected_identity_choice_lookup ON matching_order_protected_identity_choice (check_id, dataset_id, target_field, cause, ordinal)"
            )
            connection.execute("UPDATE schema_version SET version = 1")
            connection.execute(
                "INSERT INTO matching_order_protected_identity_choice VALUES ('check', 0, 'dataset', 'parent_id', 'x.parent', '[\"code\"]', '[\"SECRET\"]', 'missing_target', 0, 0)"
            )
            connection.execute(
                "INSERT INTO matching_order_protected_snapshot VALUES ('check', 'immutable-hash', 'immutable-evidence')"
            )
            before = connection.execute(
                "SELECT * FROM matching_order_protected_identity_choice"
            ).fetchall()
            upgrade = WORKSPACE_ENGINE_UPGRADES[1]

            def failed(current):
                upgrade.apply(current)
                raise RuntimeError("injected after structural changes")

            with patch.dict(
                WORKSPACE_ENGINE_UPGRADES,
                {1: ForwardSchemaUpgrade(upgrade.migration_id, failed)},
            ):
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM matching_order_protected_identity_choice"
                ).fetchall(),
                before,
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )
            schema._ensure_workspace_database_schema(connection)
            schema._ensure_workspace_database_schema(connection)
            after = connection.execute(
                "SELECT * FROM matching_order_protected_identity_choice"
            ).fetchall()
            self.assertEqual([row[:10] for row in after], before)
            self.assertEqual(after[0][10:], ("", "", 0, 0))
            self.assertEqual(
                connection.execute(
                    "SELECT * FROM matching_order_protected_snapshot"
                ).fetchall(),
                [("check", "immutable-hash", "immutable-evidence")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT from_version, to_version FROM schema_migration"
                ).fetchall(),
                [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7)],
            )
