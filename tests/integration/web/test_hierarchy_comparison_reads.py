"""Bound hierarchy comparison reads while preserving source evidence checks."""

from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.shared.performance import OperationPerformance, bind_performance
from impodo.application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from impodo.domain.project.foundation import MigrationConflictError
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyComparisonReadTests(HierarchyWorkflowFixture):
    def test_odoo19_local_comparison_reuses_verified_local_inputs(self):
        self.exercise_comparison(19, "LOCAL")

    def test_odoo20_local_comparison_reuses_verified_local_inputs(self):
        self.exercise_comparison(20, "LOCAL")

    def test_odoo19_remote_comparison_reuses_verified_local_inputs(self):
        self.exercise_comparison(19, "REMOTE")

    def test_odoo20_remote_comparison_reuses_verified_local_inputs(self):
        self.exercise_comparison(20, "REMOTE")

    def exercise_comparison(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        run_local_operation_sync(self.approve, workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )
        events = []
        collector = OperationPerformance(
            operation_id=str(uuid4()), operation="preflight", sink=events.append
        )

        def read(requirements):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            return target.compare(requirements)

        workspaces = self.context.preflight.workspaces
        with (
            patch.object(workspaces, "get", wraps=workspaces.get) as state_reads,
            count_duckdb_statements() as statements,
            bind_performance(collector),
        ):
            result = self.context.preflight.compare(
                workspace_id,
                reader=read,
                actor=self.actor,
                local_operation=run_local_operation_sync,
            )
        collector.finish(outcome="succeeded")
        self.assertEqual((result.create_count, result.unchanged_count), (13, 5))
        # One verification read and one fresh registration-artifact read.
        self.assertEqual(state_reads.call_count, 2)
        package_reads = sum("FROM source_package_file" in sql for sql in statements)
        self.assertLessEqual(package_reads, 4)
        # Fixed validation, mapping, source-view and publication work for
        # this two-dataset fixture; the previous path executed 169 statements.
        self.assertLessEqual(len(statements), 143)
        self.assertLessEqual(sum("duckdb_columns()" in sql for sql in statements), 6)
        self.assertEqual(events[-1]["counters"]["source_package_reads"], package_reads)
        self.assertIn("source_package", events[-1]["phases"])
        self.assertIsNone(getattr(_read_databases, "connections", None))
        self.assertEqual(target.writes, [])


class SourceProjectionReadTests(HierarchyWorkflowFixture):
    def test_each_source_view_uses_the_package_already_verified_for_its_projection(
        self,
    ):
        workspace_id, _ = self.hierarchy()
        foundation = self.context.workspace_states.repository.foundation
        sources = self.context.preflight.sources
        projection = WorkspaceMappingSourceProjection(foundation)
        for method in (
            sources.get_mapping_source_selection,
            projection.get_source_selection,
            projection.get_current_source_snapshots,
        ):
            with (
                self.subTest(view=method.__name__),
                patch.object(
                    foundation,
                    "get_source_package",
                    wraps=foundation.get_source_package,
                ) as packages,
                count_duckdb_statements() as statements,
            ):
                value = run_local_operation_sync(method, workspace_id)
                self.assertTrue(value)
                self.assertEqual(packages.call_count, 1)
                self.assertEqual(
                    sum("FROM source_package_file" in sql for sql in statements), 1
                )

    def test_next_source_read_rechecks_package_hash_after_a_successful_read(self):
        workspace_id, _ = self.hierarchy()
        foundation = self.context.workspace_states.repository.foundation
        sources = self.context.preflight.sources
        self.assertIsNotNone(
            run_local_operation_sync(sources.get_source_selection, workspace_id)
        )
        workspace = foundation.get_migration_workspace(workspace_id)
        version = foundation._get_data_version_registry(workspace.data_version_id)
        path = foundation.database.ensure_data_version_store(version)
        with foundation.database.connect(path) as connection:
            connection.execute(
                "UPDATE source_package_state SET package_hash = ? WHERE singleton_id = 1",
                ["sha256:" + "0" * 64],
            )
        with self.assertRaisesRegex(MigrationConflictError, "hash is inconsistent"):
            run_local_operation_sync(sources.get_source_selection, workspace_id)

    def test_combined_projection_read_rejects_a_changed_dataset_reference(self):
        workspace_id, _ = self.hierarchy()
        foundation = self.context.workspace_states.repository.foundation
        current = run_local_operation_sync(
            foundation.get_workspace_source_projection_with_package, workspace_id
        )
        self.assertIsNotNone(current)
        projection, package = current
        self.assertEqual(projection.package_hash, package.content_hash)
        self.assertEqual(
            foundation.get_workspace_source_projection(workspace_id), projection
        )
        workspace = foundation.get_migration_workspace(workspace_id)
        path = foundation.database.ensure_workspace_store(workspace)
        with foundation.database.connect(path) as connection:
            connection.execute(
                "UPDATE workspace_source_dataset SET snapshot_hash = ?",
                ["sha256:" + "0" * 64],
            )
        with self.assertRaisesRegex(MigrationConflictError, "snapshot reference"):
            run_local_operation_sync(
                foundation.get_workspace_source_projection_with_package, workspace_id
            )
