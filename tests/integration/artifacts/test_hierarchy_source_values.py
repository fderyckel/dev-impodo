"""Read complete generic hierarchy values with bounded SQL and shared semantics."""

import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb import lookup_preparation_source as producer
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.mapping_source_values import JoinedMappingSourceValues
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.hierarchy import HierarchyParentPolicy
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import DerivedSourceBinding
from impodo.domain.workspace.contracts import SourceDatasetColumn
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    _rule_payload,
    derived_dataset_links,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.artifacts.test_grouped_exact_join_preparation import (
    _publish_snapshot,
    _selection,
)
from tests.support.database_probe import count_duckdb_statements


class HierarchySourceValuesTests(TestCase):
    def fixture(self, rows, *, company=None, split=False, missing_parent="block"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        database = DuckDbWorkspaceDatabase(root / "database")
        artifacts = LocalArtifactStore(root / "artifacts")
        physical = _selection()
        source = replace(physical.datasets[0], row_count=len(rows))
        physical = replace(physical, datasets=(source,))
        snapshot = _publish_snapshot(artifacts, physical, source, rows=rows)
        if split:
            rule = DerivedEntityRule(
                str(uuid4()),
                "generated_groups",
                source.dataset_id,
                "name",
                "account.group",
                "name",
                "fictional",
                parent_separator="/",
            )
        else:
            rule = HierarchicalLookupRule(
                str(uuid4()),
                "generated_groups",
                source.dataset_id,
                ("key", "name"),
                "x.catalog.group",
                "name",
                "fictional",
                HierarchyValuePolicy(missing_parent),
                existing_parent=HierarchyParentPolicy(
                    parent_field="x_parent_id",
                    fixed_path=("Global", "Catalog"),
                    company_field="x_company_id",
                    company_name=company,
                    fixed_companies=(None, company),
                )
                if company
                else None,
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
        keys = tuple(
            key
            for key in (
                link.canonical_key_column_key,
                link.name_column_key,
                link.parent_key_column_key,
                link.company_column_key,
            )
            if key is not None
        )
        output = replace(
            source,
            dataset_id=link.derived_dataset_id,
            name=rule.output_dataset_name,
            source=DerivedSourceBinding(
                content_hash(_rule_payload(rule)),
                (source.dataset_id,),
                source.source_evidence_hash,
                hierarchy_parent=rule.existing_parent
                if isinstance(rule, HierarchicalLookupRule)
                else None,
            ),
            columns=tuple(
                SourceDatasetColumn(i, f"Value {i}", key, "string")
                for i, key in enumerate(keys, 1)
            ),
        )
        selection = replace(physical, datasets=(output,))
        sources = SimpleNamespace(
            get_source_selection=lambda _: physical,
            get_current_source_snapshots=lambda _: (snapshot,),
        )
        derivations = SimpleNamespace(get_derived_entity_plan=lambda _: plan)
        executor = DuckDbGroupedExactJoinSource(database)
        reader = JoinedMappingSourceValues(sources, derivations, artifacts, executor)
        return (
            reader,
            executor,
            artifacts,
            physical,
            selection,
            output,
            rule,
            link,
            snapshot,
            plan,
        )

    def test_generic_models_and_company_scopes_keep_projection_and_preparation_equal(
        self,
    ):
        for company, split in (
            (None, False),
            ("North", False),
            ("South", False),
            (None, True),
        ):
            with self.subTest(company=company, split=split):
                rows = (
                    ((1, "Beta / Shared"), (2, "Alpha / Shared"))
                    if split
                    else (
                        ("Beta", "Shared"),
                        ("Alpha", "Shared"),
                        ("Alpha", None),
                        (None, "Blocked"),
                    )
                )
                (
                    reader,
                    executor,
                    artifacts,
                    physical,
                    selection,
                    output,
                    rule,
                    link,
                    snapshot,
                    plan,
                ) = self.fixture(
                    rows,
                    company=company,
                    split=split,
                )
                keys = tuple(column.stable_key for column in output.columns)
                self.assertTrue(
                    reader.supports(plan.workspace_id, selection, output.dataset_id)
                )
                frame = reader.project(
                    plan.workspace_id, selection, output.dataset_id, keys
                )
                with artifacts.materialize_source_snapshot(
                    physical.data_version_id,
                    snapshot.parquet_storage_key,
                    expected_sha256=snapshot.parquet_sha256,
                ) as path:
                    batches = tuple(
                        executor.iter_lookup_batches(
                            rule=rule,
                            link=link,
                            effective_dataset=output,
                            snapshot=snapshot,
                            path=path,
                            batch_size=2,
                        )
                    )
                prepared = [
                    tuple(row.values[col.source_name] for col in output.columns)
                    for batch in batches
                    for row in batch.rows
                ]
                self.assertEqual(frame.rows(), prepared)
                self.assertEqual(
                    frame[link.name_column_key].to_list().count("Shared"), 2
                )
                self.assertEqual(
                    frame[link.name_column_key].to_list()[:2],
                    ["Global", "Catalog"] if company else ["Alpha", "Beta"],
                )
                if company:
                    self.assertEqual(
                        frame[link.company_column_key].to_list(), [None] + [company] * 5
                    )
                self.assertEqual(
                    [row.number for batch in batches for row in batch.rows],
                    list(range(2, len(prepared) + 2)),
                )

    def test_repeated_paths_normalize_once_and_counts_never_build_lineage(self):
        rows = (("Alpha", "Shared"), ("Beta", "Shared")) * 3000
        (
            reader,
            _executor,
            _artifacts,
            _physical,
            selection,
            output,
            _rule,
            link,
            _snapshot,
            plan,
        ) = self.fixture(rows)
        with (
            patch.object(
                producer,
                "evaluate_hierarchy_path",
                wraps=producer.evaluate_hierarchy_path,
            ) as evaluate,
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            counts = reader.counts(
                plan.workspace_id,
                selection,
                output.dataset_id,
                link.name_column_key,
                10,
            )
        self.assertEqual(counts, {"Alpha": 1, "Beta": 1, "Shared": 2})
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(sum("read_parquet" in sql for sql in statements), 1)
        self.assertFalse(
            any("list(DISTINCT input.source_row" in sql for sql in statements)
        )
        # One scan, row validation, distinct path grouping, normalized table,
        # two bounded path-page reads, one insert, generated records, cap, count.
        self.assertEqual(len(statements), 10)
        self.assertIn("LIMIT ?", statements[-1])

    def test_all_blank_missing_parent_unicode_aliases_and_lineage(self):
        rows = (("Straße", "Leaf"), ("STRASSE", "Leaf"), (None, "Orphan"), (None, None))
        (
            reader,
            executor,
            artifacts,
            physical,
            selection,
            output,
            rule,
            link,
            snapshot,
            plan,
        ) = self.fixture(
            rows,
            missing_parent="promote",
        )
        frame = reader.project(
            plan.workspace_id, selection, output.dataset_id, (link.name_column_key,)
        )
        self.assertEqual(frame.rows(), [("Orphan",), ("Straße",), ("Leaf",)])
        with artifacts.materialize_source_snapshot(
            physical.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            batches = tuple(
                executor.iter_lookup_batches(
                    rule=rule,
                    link=link,
                    effective_dataset=output,
                    snapshot=snapshot,
                    path=path,
                    batch_size=2,
                )
            )
        lineage = [
            sources[physical.datasets[0].dataset_id]
            for batch in batches
            for sources in batch.physical_sources
        ]
        self.assertEqual(lineage, [(3,), (1, 2), (1, 2)])
        self.assertEqual(
            [bool(issues) for batch in batches for issues in batch.issues],
            [False, True, True],
        )

    def test_changed_bindings_bytes_and_budget_reject_without_partial_values(self):
        (
            reader,
            _executor,
            artifacts,
            physical,
            selection,
            output,
            _rule,
            link,
            snapshot,
            plan,
        ) = self.fixture((("A", "Leaf"),))
        keys = (link.name_column_key,)
        with self.assertRaisesRegex(WorkspaceError, "source evidence changed"):
            reader.project(
                plan.workspace_id,
                replace(selection, data_version_id=str(uuid4())),
                output.dataset_id,
                keys,
            )
        with patch.object(producer, "LOOKUP_DERIVED_OUTPUT_ROW_LIMIT", 1):
            with self.assertRaises(WorkspaceError):
                reader.project(plan.workspace_id, selection, output.dataset_id, keys)
        with artifacts.materialize_source_snapshot(
            physical.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            with self.assertRaisesRegex(SourceLoadError, "source rows changed"):
                tuple(
                    producer.iter_lookup_batches(
                        _executor._connection_factory,
                        rule=_rule,
                        link=link,
                        effective_dataset=output,
                        snapshot=replace(
                            snapshot,
                            row_count=snapshot.row_count + 1,
                            logical_hash="",
                            parquet_storage_key="",
                            _calculate_bindings=True,
                        ),
                        path=path,
                        batch_size=2,
                    )
                )
        broken = replace(
            output, source=replace(output.source, rule_hash=content_hash("changed"))
        )
        with self.assertRaisesRegex(WorkspaceError, "source evidence changed"):
            reader.project(
                plan.workspace_id,
                replace(selection, datasets=(broken,)),
                output.dataset_id,
                keys,
            )

    def test_two_hierarchies_keep_their_own_company_in_one_plan(self):
        (
            reader,
            _executor,
            _artifacts,
            _physical,
            selection,
            output,
            rule,
            link,
            _snapshot,
            plan,
        ) = self.fixture(
            (("Alpha", "Shared"),),
            company="North",
        )
        other_rule = replace(
            rule,
            rule_id=str(uuid4()),
            output_dataset_name="other_groups",
            existing_parent=replace(
                rule.existing_parent,
                company_name="South",
                fixed_companies=(None, "South"),
            ),
        )
        combined_plan = replace(plan, rules=(rule, other_rule))
        other_link = derived_dataset_links(combined_plan)[1]
        other_keys = (
            other_link.canonical_key_column_key,
            other_link.name_column_key,
            other_link.parent_key_column_key,
            other_link.company_column_key,
        )
        other_output = replace(
            output,
            dataset_id=other_link.derived_dataset_id,
            name=other_rule.output_dataset_name,
            source=replace(
                output.source,
                rule_hash=content_hash(_rule_payload(other_rule)),
                hierarchy_parent=other_rule.existing_parent,
            ),
            columns=tuple(
                replace(column, stable_key=key)
                for column, key in zip(output.columns, other_keys, strict=True)
            ),
        )
        combined_selection = replace(selection, datasets=(output, other_output))
        reader.derivations = SimpleNamespace(
            get_derived_entity_plan=lambda _: combined_plan
        )
        for dataset, binding, company in (
            (output, link, "North"),
            (other_output, other_link, "South"),
        ):
            frame = reader.project(
                plan.workspace_id,
                combined_selection,
                dataset.dataset_id,
                (binding.name_column_key, binding.company_column_key),
            )
            self.assertEqual(
                frame.rows(),
                [
                    ("Global", None),
                    ("Catalog", company),
                    ("Alpha", company),
                    ("Shared", company),
                ],
            )
