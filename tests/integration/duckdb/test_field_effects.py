"""Prove complete counts, legacy upgrade, rollback, and value-free paging."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.schema.forward_upgrades import ForwardSchemaUpgrade
from impodo.adapters.duckdb.schema.workspace_engine import (
    WORKSPACE_ENGINE_UPGRADES,
    WorkspaceEngineSchemaMixin,
)
from impodo.adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    _TransformationImpactCollector,
)
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateCompatibilityError,
    WorkspaceStatus,
)


class FieldEffectsRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = DuckDbWorkspaceDatabase(Path(temporary.name))
        self.workspace_id = str(uuid4())
        WorkspaceStateRepository(database).initialize_workbench(
            WorkspaceState(
                workspace_id=self.workspace_id,
                name="Field counts",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
            ),
            actor=LOCAL_ACTOR,
        )
        self.repository = TransformationImpactRepository(database)

    def test_complete_field_counts_match_rows_and_report_and_reject_partial_replacement(
        self,
    ):
        identity = TransformationImpactIdentity(
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            None,
        )

        def build(sink):
            collector = _TransformationImpactCollector(
                identity.mapping_content_hash, detail_limit=0, sink=sink
            )
            for field in ("name", "code"):
                for index, outcome in enumerate(
                    ("changed", "fallback", "invalid", "null", "provided", "unchanged")
                ):
                    collector.record(
                        dataset="Products",
                        source_row=index + 2,
                        source_column="Private column",
                        target_field=field,
                        raw_value="Private original",
                        proposed_value="Private prepared",
                        rules="Source",
                        outcome=outcome,
                    )
            return collector.report()

        snapshot = self.repository.replace_transformation_impact_snapshot(
            self.workspace_id, identity, build, actor=LOCAL_ACTOR
        )
        stored = self.repository.get_transformation_impact_snapshot(
            self.workspace_id, identity
        )
        self.assertEqual(stored.report.field_impacts, snapshot.report.field_impacts)
        rows = tuple(
            self.repository.iter_transformation_impact_rows(
                self.workspace_id, identity, TransformationImpactFilter()
            )
        )
        projection = self.repository.get_transformation_field_effects(
            self.workspace_id, identity, (("Products", "name"),)
        )
        self.assertTrue(projection.current)
        self.assertEqual(len(projection.fields), 1)
        counts = projection.fields[0].counts
        for outcome in ("changed", "fallback", "invalid", "null", "provided"):
            self.assertEqual(
                getattr(counts, outcome + "_count"),
                sum(
                    row.outcome == outcome and row.target_field == "name"
                    for row in rows
                ),
            )
        self.assertEqual(counts.unchanged_count, 1)
        self.assertNotIn("Private", repr(projection))
        newer = replace(identity, mapping_content_hash="sha256:" + "9" * 64)
        self.assertFalse(
            self.repository.get_transformation_field_effects(
                self.workspace_id, newer, (("Products", "name"),)
            ).current
        )

        def incomplete(sink):
            report = build(sink)
            return replace(
                report,
                mapping_content_hash=newer.mapping_content_hash,
                field_impacts=report.field_impacts[:1],
            )

        with self.assertRaisesRegex(Exception, "do not reconcile"):
            self.repository.replace_transformation_impact_snapshot(
                self.workspace_id, newer, incomplete, actor=LOCAL_ACTOR
            )
        self.assertEqual(
            self.repository.get_transformation_impact_snapshot(
                self.workspace_id, identity
            ).report.field_impacts,
            snapshot.report.field_impacts,
        )


class FieldEffectsUpgradeTests(unittest.TestCase):
    def test_v5_upgrade_is_atomic_and_keeps_old_counts_unknown(self):
        schema = WorkspaceEngineSchemaMixin()
        with duckdb.connect(":memory:") as connection:
            schema._initialize_workspace_database(connection)
            connection.execute(
                "ALTER TABLE transformation_impact_run DROP COLUMN field_counts_json"
            )
            connection.execute("UPDATE schema_version SET version=5")
            upgrade = WORKSPACE_ENGINE_UPGRADES[5]

            def fail(current):
                upgrade.apply(current)
                raise RuntimeError("injected after adding metadata")

            with patch.dict(
                WORKSPACE_ENGINE_UPGRADES,
                {5: ForwardSchemaUpgrade(upgrade.migration_id, fail)},
            ):
                with self.assertRaises(WorkspaceStateCompatibilityError):
                    schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (5,),
            )
            self.assertNotIn(
                "field_counts_json",
                [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('transformation_impact_run')"
                    ).fetchall()
                ],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM schema_migration").fetchall(), []
            )
            schema._ensure_workspace_database_schema(connection)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone(),
                (6,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT field_counts_json FROM transformation_impact_run"
                ).fetchall(),
                [],
            )
