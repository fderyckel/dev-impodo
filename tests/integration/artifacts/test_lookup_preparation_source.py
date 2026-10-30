"""Compare narrow lookup production with the authoritative Unicode grouping."""

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import DerivedSourceBinding
from impodo.domain.workspace.contracts import SourceDatasetColumn
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    _group_lookup_values,
    derived_dataset_links,
)
from tests.integration.artifacts.test_grouped_exact_join_preparation import (
    _publish_snapshot,
    _selection,
)


class LookupPreparationSourceTests(TestCase):
    def _fixture(self, values):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = DuckDbWorkspaceDatabase(root / "database")
        artifacts = LocalArtifactStore(root / "artifacts")
        physical = _selection()
        source = replace(physical.datasets[1], row_count=len(values))
        physical = replace(physical, datasets=(source,))
        snapshot = _publish_snapshot(
            artifacts, physical, source, rows=tuple(enumerate(values, 1))
        )
        rule = DerivedEntityRule(
            str(uuid4()),
            "reusable_labels",
            source.dataset_id,
            "region",
            "x_custom.label",
            "name",
            "fixture",
        )
        plan = DerivedEntityPlan(
            str(uuid4()),
            1,
            str(uuid4()),
            physical.content_hash,
            (rule,),
            physical.created_at,
            "Fixture",
        )
        link = derived_dataset_links(plan)[0]
        output = replace(
            source,
            dataset_id=link.derived_dataset_id,
            name=rule.output_dataset_name,
            source=DerivedSourceBinding(
                content_hash("rule"), (source.dataset_id,), source.source_evidence_hash
            ),
            columns=(
                SourceDatasetColumn(1, "Key", link.canonical_key_column_key, "string"),
                SourceDatasetColumn(2, "Label", link.name_column_key, "string"),
            ),
        )
        return database, artifacts, physical, source, snapshot, rule, link, output

    def test_unicode_alias_blank_and_lineage_match_complete_grouping(self):
        values = (
            "  Café  Group ",
            "Café Group",
            "Ｃａｆé Group",
            "CAFÉ GROUP",
            None,
            "\t",
            "Straße",
            "STRASSE",
            "Beta",
            False,
            0,
            12,
        )
        database, artifacts, physical, source, snapshot, rule, link, output = (
            self._fixture(values)
        )
        with artifacts.materialize_source_snapshot(
            physical.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            with collect_duckdb_request_timings() as timing:
                batches = tuple(
                    DuckDbGroupedExactJoinSource(database).iter_lookup_batches(
                        rule=rule,
                        link=link,
                        effective_dataset=output,
                        snapshot=snapshot,
                        path=path,
                        batch_size=2,
                    )
                )
        expected = _group_lookup_values(rule, enumerate(values, 1)).groups
        rows = [
            (row, lineage, issues)
            for batch in batches
            for row, lineage, issues in zip(
                batch.rows, batch.physical_sources, batch.issues, strict=True
            )
        ]
        self.assertEqual(len(rows), len(expected))
        for index, ((row, lineage, issues), group) in enumerate(
            zip(rows, expected, strict=True), 2
        ):
            self.assertEqual(row.number, index)
            self.assertEqual(
                row.values, {"Key": group.key_path[0], "Label": group.name}
            )
            self.assertEqual(lineage, {source.dataset_id: group.source_rows})
            self.assertEqual(bool(issues), len(group.aliases) > 1)
        self.assertEqual(timing.connection_count, 1)
        self.assertEqual(timing.schema_check_count, 0)

    def test_normalization_runs_per_distinct_value_and_generated_budget_is_guarded(
        self,
    ):
        fixture = self._fixture(("Alpha", "Beta") * 2000)
        database, artifacts, physical, _source, snapshot, rule, link, output = fixture
        from impodo.adapters.duckdb import lookup_preparation_source as module

        with artifacts.materialize_source_snapshot(
            physical.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            with patch.object(
                module, "_normalized_path", wraps=module._normalized_path
            ) as normalize:
                batches = tuple(
                    DuckDbGroupedExactJoinSource(database).iter_lookup_batches(
                        rule=rule,
                        link=link,
                        effective_dataset=output,
                        snapshot=snapshot,
                        path=path,
                        batch_size=1000,
                    )
                )
            self.assertEqual(normalize.call_count, 2)
            self.assertEqual(
                sum(
                    len(rows)
                    for batch in batches
                    for sources in batch.physical_sources
                    for rows in sources.values()
                ),
                4000,
            )
            with patch.object(module, "LOOKUP_DERIVED_OUTPUT_ROW_LIMIT", 1):
                with self.assertRaisesRegex(SourceLoadError, "generated-record budget"):
                    tuple(
                        DuckDbGroupedExactJoinSource(database).iter_lookup_batches(
                            rule=rule,
                            link=link,
                            effective_dataset=output,
                            snapshot=snapshot,
                            path=path,
                            batch_size=1000,
                        )
                    )
