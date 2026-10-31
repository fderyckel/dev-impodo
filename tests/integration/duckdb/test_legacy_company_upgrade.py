"""Keep saved evidence when upgrading the earlier company-column layout."""

from __future__ import annotations

import unittest
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.schema.business_intent_assessment import (
    BUSINESS_ASSESSMENT_TABLES,
)
from impodo.adapters.duckdb.schema.legacy_company import LEGACY_COMPANY_MIGRATION_ID
from impodo.adapters.duckdb.schema.structure import read_table_columns
from impodo.adapters.duckdb.schema.workspace_engine import WorkspaceEngineSchemaMixin
from impodo.adapters.duckdb.serialization import _workspace_values
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.shared.schema import WORKSPACE_SCHEMA_VERSION
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStateCompatibilityError,
)


class LegacyCompanyUpgradeTests(unittest.TestCase):
    def legacy(self, *, company=None):
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        schema = WorkspaceEngineSchemaMixin()
        schema._initialize_workspace_database(connection)
        state = WorkspaceState(
            str(uuid4()),
            "Fictional delivery",
            "CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://fictional.example.test",
            odoo_database="fixture",
        )
        values = _workspace_values(state)
        connection.execute(
            "INSERT INTO workspace_projection_cache VALUES ("
            + ",".join("?" for _ in values)
            + ")",
            values,
        )
        for table in (*BUSINESS_ASSESSMENT_TABLES, "odoo_company_binding"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "ALTER TABLE workspace_projection_cache ADD COLUMN odoo_company_id BIGINT"
        )
        connection.execute(
            "ALTER TABLE workspace_projection_cache ADD COLUMN odoo_company_name VARCHAR DEFAULT ''"
        )
        if company is not None:
            connection.execute(
                "UPDATE workspace_projection_cache SET odoo_company_id=?, odoo_company_name=?",
                company,
            )
        connection.execute("UPDATE schema_version SET version=24")
        connection.execute(
            "INSERT INTO schema_migration VALUES (23,24,?)",
            [LEGACY_COMPANY_MIGRATION_ID],
        )
        connection.execute(
            "INSERT INTO source_selection VALUES (1, ?)",
            ['{"opaque_source":"UNCHANGED"}'],
        )
        connection.execute(
            "INSERT INTO mapping_working_draft VALUES (1, 'mapping', 1, 'source', 'schema', 'accepted', '2026-10-01T00:00:00Z', '{\"matches\":\"UNCHANGED\"}')"
        )
        connection.execute(
            "INSERT INTO execution_run (run_id,snapshot_hash,snapshot_root_hash,preflight_run_id,target_hash,target_database,status,started_at,started_by) VALUES ('run','snapshot','root','comparison','target','fixture','COMPLETED_WITH_ERRORS','2026-10-01T00:00:00Z','Fictional operator')"
        )
        connection.execute(
            "INSERT INTO execution_row VALUES ('run',1,'row','chains','x.chain','CREATE','COMMITTED',1,707,'','{\"original_case\":\"CHAIN\"}','2026-10-01T00:00:00Z')"
        )
        return schema, connection

    def evidence(self, connection):
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "source_selection",
                "mapping_working_draft",
                "execution_run",
                "execution_row",
            )
        }

    def test_known_v24_with_no_company_upgrades_once_and_keeps_saved_evidence(self):
        schema, connection = self.legacy()
        before = self.evidence(connection)
        current = connection.execute(
            "SELECT * EXCLUDE(odoo_company_id, odoo_company_name) FROM workspace_projection_cache"
        ).fetchall()
        schema._ensure_workspace_database_schema(connection)
        schema._ensure_workspace_database_schema(connection)
        self.assertEqual(self.evidence(connection), before)
        self.assertEqual(
            connection.execute("SELECT * FROM workspace_projection_cache").fetchall(),
            current,
        )
        self.assertEqual(
            connection.execute("SELECT version FROM schema_version").fetchone()[0],
            WORKSPACE_SCHEMA_VERSION,
        )
        self.assertEqual(
            connection.execute("SELECT * FROM odoo_company_binding").fetchall(), []
        )
        self.assertEqual(
            connection.execute(
                "SELECT run_id, assessment_hash, approval_json FROM business_intent_approval"
            ).fetchall(),
            [("run", None, None)],
        )
        self.assertEqual(
            connection.execute(
                "SELECT from_version,to_version,migration_id FROM schema_migration ORDER BY to_version"
            ).fetchall(),
            [
                (23, 24, LEGACY_COMPANY_MIGRATION_ID),
                (24, 25, "workspace-engine-v24-to-v25-business-intent-assessment"),
                (25, 26, "workspace-engine-v25-to-v26-legacy-company-layout"),
            ],
        )

    def test_saved_legacy_choice_is_retained_as_protected_evidence(self):
        schema, connection = self.legacy(company=(809, "Fictional Factory"))
        before = self.evidence(connection)
        schema._ensure_workspace_database_schema(connection)
        row = connection.execute(
            "SELECT binding_hash,selection_json FROM odoo_company_binding"
        ).fetchone()
        selection = OdooCompanySelection.from_json(row[1])
        self.assertEqual(row[0], selection.binding_hash)
        self.assertEqual(
            (selection.company_id, selection.name), (809, "Fictional Factory")
        )
        self.assertEqual(self.evidence(connection), before)
        self.assertNotIn(
            "odoo_company_id",
            read_table_columns(connection)["workspace_projection_cache"],
        )

    def test_unknown_or_ambiguous_layout_rolls_back_every_upgrade_step(self):
        mutations = (
            "UPDATE schema_migration SET migration_id='unknown-history'",
            "ALTER TABLE workspace_projection_cache ALTER COLUMN odoo_company_id TYPE VARCHAR",
            "ALTER TABLE workspace_projection_cache ADD COLUMN unknown_metadata VARCHAR",
            "CREATE TABLE unexpected_evidence (id INTEGER)",
            "CREATE TABLE odoo_company_binding (binding_hash VARCHAR, selection_json VARCHAR)",
            "UPDATE workspace_projection_cache SET odoo_company_name='A name without an ID'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                schema, connection = self.legacy()
                connection.execute(mutation)
                before = self.evidence(connection)
                columns = read_table_columns(connection)
                ledger = connection.execute("SELECT * FROM schema_migration").fetchall()
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
                self.assertEqual(
                    connection.execute("SELECT version FROM schema_version").fetchone(),
                    (24,),
                )
                self.assertEqual(read_table_columns(connection), columns)
                self.assertEqual(
                    connection.execute("SELECT * FROM schema_migration").fetchall(),
                    ledger,
                )
                self.assertEqual(self.evidence(connection), before)

    def test_canonical_v25_upgrades_without_reapproving_a_load(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute("UPDATE schema_version SET version=25")
            columns = read_table_columns(connection)
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(read_table_columns(connection), columns)
            self.assertEqual(
                connection.execute("SELECT * FROM business_intent_approval").fetchall(),
                [],
            )
