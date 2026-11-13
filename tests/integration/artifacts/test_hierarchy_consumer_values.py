"""Read generated references beside original frozen columns without losing rows."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.source_snapshot import SOURCE_ROW_COLUMN
from impodo.domain.workspace.contracts import SourceDatasetColumn
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.artifacts import test_hierarchy_source_values as fixtures
from tests.support.database_probe import count_duckdb_statements


class HierarchyConsumerValueTests(TestCase):
    def fixture(self):
        helper = fixtures.HierarchySourceValuesTests(methodName="runTest")
        self.addCleanup(helper.doCleanups)
        reader, _, _, physical, _, _, rule, _, snapshot, plan = helper.fixture(
            (("0100", "100"), ("0100", "101"), ("0107", "007"), (None, "blank")),
            company="North",
        )
        rule = replace(rule, source_level_column_keys=("key",))
        plan = replace(plan, rules=(rule,))
        reader.derivations = SimpleNamespace(get_derived_entity_plan=lambda _: plan)
        link = derived_dataset_links(plan)[0]
        source = physical.datasets[0]
        consumer = replace(
            source,
            columns=(
                *source.columns,
                SourceDatasetColumn(
                    len(source.columns) + 1,
                    "Selected generated_groups path",
                    link.source_column_key,
                    "string",
                ),
            ),
        )
        selection = replace(physical, datasets=(consumer,))
        return reader, physical, selection, consumer, link, snapshot, plan

    def test_parent_paths_preserve_original_rows_and_leading_zeroes(self):
        reader, _, selection, source, link, _, plan = self.fixture()
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as sql,
        ):
            frame = reader.project(
                plan.workspace_id,
                selection,
                source.dataset_id,
                ("key", link.source_column_key, SOURCE_ROW_COLUMN),
            )
        self.assertEqual(
            frame.rows(),
            [
                ("0100", '["global","catalog","0100"]', 1),
                ("0100", '["global","catalog","0100"]', 2),
                ("0107", '["global","catalog","0107"]', 3),
                (None, None, 4),
            ],
        )
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(len(sql), 1)
        self.assertNotIn('"value_2"', sql[0])
        self.assertNotIn("lookup_records", sql[0])

    def test_choices_count_every_original_row_and_exclude_blank_paths(self):
        reader, _, selection, source, link, _, plan = self.fixture()
        self.assertTrue(
            reader.supports(plan.workspace_id, selection, source.dataset_id)
        )
        self.assertEqual(
            reader.counts(
                plan.workspace_id,
                selection,
                source.dataset_id,
                link.source_column_key,
                1,
            ),
            {'["global","catalog","0100"]': 2, '["global","catalog","0107"]': 1},
        )

    def test_stale_plan_and_changed_snapshot_stay_blocked(self):
        reader, _, selection, source, link, snapshot, plan = self.fixture()
        reader.derivations = SimpleNamespace(
            get_derived_entity_plan=lambda _: replace(
                plan, source_selection_hash="changed"
            )
        )
        with self.assertRaisesRegex(WorkspaceError, "source evidence changed"):
            reader.project(
                plan.workspace_id,
                selection,
                source.dataset_id,
                (link.source_column_key,),
            )
        reader.derivations = SimpleNamespace(get_derived_entity_plan=lambda _: plan)
        with reader.artifacts.materialize_source_snapshot(
            selection.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            path.write_bytes(b"changed frozen test bytes")
        with self.assertRaises(WorkspaceError):
            reader.project(
                plan.workspace_id,
                selection,
                source.dataset_id,
                (link.source_column_key,),
            )
