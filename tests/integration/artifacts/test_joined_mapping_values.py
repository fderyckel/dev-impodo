"""Verify joined choices, generic relationship coverage, and bounded reads."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.exact_join_group_evaluator import (
    DuckDbExactJoinGroupEvaluator,
)
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.mapping_source_values import JoinedMappingSourceValues
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.derived_entities import (
    DerivedEntityWorkspaceService,
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    bounded_exact_join_group,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.serialization import content_hash
from impodo.domain.structural import (
    AggregateOperation,
    AggregateSpec,
    GroupAggregateRule,
    GroupKey,
    StructuralOutputColumn,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import (
    SchemaField,
    SchemaModel,
    SourceDatasetColumn,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    ExactJoinInputSnapshotEvidence,
    SavedExactJoinReview,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    mapping_source_selection,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.artifacts.test_grouped_exact_join_preparation import (
    _publish_snapshot,
    _selection,
)


class JoinedMappingValueTests(unittest.TestCase):
    def setUp(self):
        self._prepare_join()

    def _prepare_join(self, *, region_rows=None, row_counts=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.workspace_id = str(uuid4())
        self.database = DuckDbWorkspaceDatabase(root / "databases")
        self.artifacts = LocalArtifactStore(root / "artifacts")
        self.physical = _selection()
        if row_counts:
            self.physical = replace(
                self.physical,
                datasets=tuple(
                    replace(
                        dataset,
                        row_count=count,
                        columns=(
                            *dataset.columns,
                            *(
                                SourceDatasetColumn(
                                    index, f"Unused {index}", f"unused_{index}", "empty"
                                )
                                for index in range(
                                    3, 89 if dataset.name == "primary" else 88
                                )
                            ),
                        ),
                    )
                    for dataset, count in zip(
                        self.physical.datasets, row_counts, strict=True
                    )
                ),
            )
        source_rows = (
            {
                dataset.name: tuple(
                    (index, f"value-{index % 5}")
                    for index in range(1, dataset.row_count + 1)
                )
                for dataset in self.physical.datasets
            }
            if row_counts
            else {}
        )
        if region_rows is not None:
            source_rows["regions"] = region_rows
        self.snapshots = tuple(
            _publish_snapshot(
                self.artifacts,
                self.physical,
                dataset,
                rows=source_rows.get(dataset.name),
            )
            for dataset in self.physical.datasets
        )
        primary, region, email = self.physical.datasets
        request = ExactJoinGroupRequest(
            output_dataset_name="records_complete",
            primary_dataset_id=primary.dataset_id,
            primary_key_column_keys=("key",),
            key_value_types=("integer",),
            additions=(
                ExactJoinGroupAddition(region.dataset_id, ("key",)),
                ExactJoinGroupAddition(email.dataset_id, ("key",)),
            ),
            projections=(
                ExactJoinGroupProjection(primary.dataset_id, "key", "ID"),
                ExactJoinGroupProjection(region.dataset_id, "region", "Choice"),
                ExactJoinGroupProjection(email.dataset_id, "email", "Email"),
            ),
        )
        rules = DerivedEntityWorkspaceService._join_group_rules(
            request, self.physical, None
        )
        with ExitStack() as stack:
            paths = {
                item.dataset_id: stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        self.physical.data_version_id,
                        item.parquet_storage_key,
                        expected_sha256=item.parquet_sha256,
                    )
                )
                for item in self.snapshots
            }
            preview = DuckDbExactJoinGroupEvaluator(self.database).review(
                request=request,
                rules=rules,
                selection=self.physical,
                snapshots={item.dataset_id: item for item in self.snapshots},
                paths=paths,
            )
        self.assertTrue(preview.can_save)
        evidence = tuple(
            ExactJoinInputSnapshotEvidence(
                item.dataset_id, item.logical_hash, item.parquet_sha256
            )
            for item in self.snapshots
        )
        self.plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=self.workspace_id,
            source_selection_hash=self.physical.content_hash,
            rules=rules,
            updated_at=datetime.now(UTC),
            updated_by="Test operator",
            dataset_uses=tuple(
                SourceDatasetUsePolicy(
                    dataset.dataset_id, SourceDatasetUse.PREPARATION_ONLY
                )
                for dataset in self.physical.datasets
            ),
            join_reviews=tuple(
                SavedExactJoinReview(
                    rule.rule_id,
                    content_hash(rule.to_dict()),
                    self.physical.content_hash,
                    evidence,
                    review,
                )
                for rule, review in zip(rules, preview.reviews, strict=True)
            ),
        )
        self.effective = mapping_source_selection(self.physical, self.plan)
        self.sources = SimpleNamespace(
            get_source_selection=lambda _: self.physical,
            get_mapping_source_selection=lambda _: self.effective,
            get_current_source_snapshots=lambda _: self.snapshots,
        )
        self.derivations = SimpleNamespace(get_derived_entity_plan=lambda _: self.plan)
        self.executor = DuckDbGroupedExactJoinSource(self.database)
        self.reader = JoinedMappingSourceValues(
            self.sources, self.derivations, self.artifacts, self.executor
        )
        self.coverage = CategoricalCoverageService(
            self.sources, self.artifacts, self.reader
        )
        self.dataset = self.effective.datasets[0]
        self.key = next(
            column.stable_key
            for column in self.dataset.columns
            if column.source_name == "Choice"
        )

    def test_choices_and_generic_many2one_coverage_share_joined_values(self):
        self._assert_choices_and_coverage()

    def _assert_choices_and_coverage(self):
        choices = self.coverage.source_value_choices(
            self.workspace_id, self.dataset.dataset_id, self.key
        )
        self.assertEqual(
            choices,
            (
                {"value": "BE", "count": 1},
                {"value": "FR", "count": 1},
                {"value": "NL", "count": 1},
            ),
        )
        relation = RelationshipMapping(
            target_field="x_custom_link",
            kind="many2one",
            source_column_keys=(self.key,),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="x_custom.list",
                key_mappings=(ReferenceKeyMapping(self.key, "code"),),
                value_mappings=(
                    ValueMapping("BE", "Belgium"),
                    ValueMapping("FR", "France"),
                ),
            ),
            categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        )
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=self.effective.content_hash,
            schema_hash="sha256:" + "a" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=self.dataset.dataset_id,
                    target_model="x_custom.record",
                    relationships=(relation,),
                ),
            ),
        )
        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "x_custom.record",
                    "Record",
                    (
                        SchemaField(
                            "x_custom_link",
                            "List",
                            "many2one",
                            False,
                            False,
                            "x_custom.list",
                            None,
                            (),
                        ),
                    ),
                ),
            )
        )
        incomplete = self.coverage.collect(
            self.workspace_id, definition, self.effective, schema
        )
        self.assertEqual(
            incomplete.evidence.field_results[0].uncovered_values, (("NL",),)
        )
        self.assertEqual(
            incomplete.issues[0].code, "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE"
        )
        complete = replace(
            relation,
            resolver=replace(
                relation.resolver,
                value_mappings=(
                    *relation.resolver.value_mappings,
                    ValueMapping("NL", "Netherlands"),
                ),
            ),
        )
        definition = replace(
            definition,
            datasets=(replace(definition.datasets[0], relationships=(complete,)),),
        )
        checked = self.coverage.collect(
            self.workspace_id, definition, self.effective, schema
        )
        self.assertEqual(checked.issues, ())
        self.assertTrue(checked.evidence.recipe_eligible)
        self.assertEqual(len(checked.evidence.source_snapshot_hashes), 3)
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)

    def test_join_reader_projects_only_keys_and_requested_column_once(self):
        from impodo.adapters.duckdb import grouped_exact_join_source as module

        with (
            patch.object(
                module, "_create_narrow_input", wraps=module._create_narrow_input
            ) as scan,
            collect_duckdb_request_timings() as timings,
        ):
            frame = self.reader.project(
                self.workspace_id, self.effective, self.dataset.dataset_id, (self.key,)
            )
        self.assertEqual(frame[self.key].to_list(), ["BE", "FR", "NL"])
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(scan.call_count, 3)
        self.assertEqual(
            [call.kwargs["needed_column_keys"] for call in scan.call_args_list],
            [("key",), ("key", "region"), ("key",)],
        )
        counts = self.reader.counts(
            self.workspace_id, self.effective, self.dataset.dataset_id, self.key, 1
        )
        self.assertEqual(len(counts), 2)
        with patch(
            "impodo.application.workspace.mapping.categorical_coverage.MAX_VALUE_MAPPINGS", 1,
        ), self.assertRaisesRegex(WorkspaceError, "too many distinct"):
            self.coverage.source_value_choices(
                self.workspace_id, self.dataset.dataset_id, self.key,
            )

    def test_stale_join_review_is_rejected_by_choices_and_validation(self):
        self.plan = replace(
            self.plan,
            join_reviews=(
                replace(
                    self.plan.join_reviews[0],
                    input_snapshots=(
                        replace(
                            self.plan.join_reviews[0].input_snapshots[0],
                            logical_hash="sha256:" + "f" * 64,
                        ),
                        *self.plan.join_reviews[0].input_snapshots[1:],
                    ),
                ),
                *self.plan.join_reviews[1:],
            ),
        )
        with self.assertRaisesRegex(WorkspaceError, "stale"):
            self.coverage.source_value_choices(
                self.workspace_id, self.dataset.dataset_id, self.key
            )
        with self.assertRaisesRegex(WorkspaceError, "stale"):
            self.reader.project(
                self.workspace_id, self.effective, self.dataset.dataset_id, (self.key,)
            )

    def test_choices_count_joined_rows_and_preserve_blank_and_whitespace_semantics(
        self,
    ):
        self._prepare_join(region_rows=((1, "\tBE\xa0"), (2, " \t "), (99, "Unused")))
        choices = self.coverage.source_value_choices(
            self.workspace_id, self.dataset.dataset_id, self.key
        )
        self.assertEqual(choices, ({"value": "BE", "count": 1},))
        values = self.reader.project(
            self.workspace_id, self.effective, self.dataset.dataset_id, (self.key,)
        )
        self.assertEqual(values[self.key].to_list(), ["\tBE\xa0", " \t ", None])

    def test_motivating_three_table_scale_counts_only_final_primary_rows(self):
        self._prepare_join(row_counts=(4753, 44387, 16159))
        self.assertEqual(
            sum(len(dataset.columns) for dataset in self.physical.datasets), 262
        )
        with collect_duckdb_request_timings() as timings:
            choices = self.coverage.source_value_choices(
                self.workspace_id, self.dataset.dataset_id, self.key
            )
        self.assertEqual(sum(item["count"] for item in choices), 4753)
        self.assertEqual(len(choices), 5)
        self.assertEqual(timings.connection_count, 1)

    def test_joined_choices_work_when_an_input_is_also_mapped_directly(self):
        self.plan = replace(self.plan, dataset_uses=tuple(
            replace(item, use=SourceDatasetUse.MAP_AND_PREPARE)
            if item.dataset_id == self.physical.datasets[0].dataset_id else item
            for item in self.plan.dataset_uses
        ))
        self.effective = mapping_source_selection(self.physical, self.plan)
        self.assertEqual(len(self.effective.datasets), 2)
        self.assertIsNone(bounded_exact_join_group(self.physical, self.effective, self.plan))
        self._assert_choices_and_coverage()
        with collect_duckdb_request_timings() as timings:
            choices = self.coverage.source_value_choices(
                self.workspace_id, self.dataset.dataset_id, self.key,
            )
        self.assertEqual(choices, (
            {"value": "BE", "count": 1}, {"value": "FR", "count": 1},
            {"value": "NL", "count": 1},
        ))
        self.assertEqual(timings.connection_count, 1)

    def test_independent_join_groups_read_only_the_selected_group(self):
        from impodo.adapters.duckdb import grouped_exact_join_source as module

        _primary, region, email = self.physical.datasets
        request = ExactJoinGroupRequest(
            output_dataset_name="other_records", primary_dataset_id=region.dataset_id,
            primary_key_column_keys=("key",), key_value_types=("integer",),
            additions=(ExactJoinGroupAddition(email.dataset_id, ("key",)),),
            projections=(ExactJoinGroupProjection(region.dataset_id, "region", "Choice"),),
        )
        rules = DerivedEntityWorkspaceService._join_group_rules(request, self.physical, self.plan)
        with ExitStack() as stack:
            paths = {snapshot.dataset_id: stack.enter_context(self.artifacts.materialize_source_snapshot(
                self.physical.data_version_id, snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            )) for snapshot in self.snapshots if snapshot.dataset_id in {region.dataset_id, email.dataset_id}}
            preview = DuckDbExactJoinGroupEvaluator(self.database).review(request=request, rules=rules,
                selection=self.physical, snapshots={item.dataset_id: item for item in self.snapshots}, paths=paths)
        self.assertTrue(preview.can_save)
        self.plan = replace(self.plan, rules=(*self.plan.rules, *rules), join_reviews=(
            *self.plan.join_reviews,
            SavedExactJoinReview(rules[0].rule_id, content_hash(rules[0].to_dict()), self.physical.content_hash,
                self.plan.join_reviews[0].input_snapshots, preview.reviews[0]),
        ))
        self.effective = mapping_source_selection(self.physical, self.plan)
        other = next(item for item in self.effective.datasets if item.dataset_id == structural_dataset_id(rules[0]))
        self.assertIsNone(bounded_exact_join_group(self.physical, self.effective, self.plan))
        with patch.object(module, "_create_narrow_input", wraps=module._create_narrow_input) as scan, collect_duckdb_request_timings() as timings:
            choices = self.reader.counts(self.workspace_id, self.effective, other.dataset_id, other.columns[0].stable_key, 1000)
        self.assertEqual(choices, {"BE": 1, "FR": 1, "NL": 1})
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(timings.connection_count, 1)
        self.plan = replace(self.plan, join_reviews=tuple(
            replace(review, input_snapshots=(replace(review.input_snapshots[0], logical_hash="sha256:" + "f" * 64), *review.input_snapshots[1:]))
            if review.rule_id == self.plan.rules[0].rule_id else review
            for review in self.plan.join_reviews
        ))
        with self.assertRaisesRegex(WorkspaceError, "stale"):
            self.reader.counts(self.workspace_id, self.effective, self.dataset.dataset_id, self.key, 1000)
        self.assertEqual(self.reader.counts(self.workspace_id, self.effective, other.dataset_id, other.columns[0].stable_key, 1000), choices)

    def test_other_preparation_rules_do_not_disable_joined_choices_or_validation(self):
        rule = GroupAggregateRule(
            rule_id=str(uuid4()), output_dataset_name="region_counts",
            source_dataset_id=self.physical.datasets[1].dataset_id,
            output_columns=(StructuralOutputColumn("region", "Region", "string"),
                StructuralOutputColumn("count", "Count", "integer")),
            group_keys=(GroupKey("region", "region"),),
            aggregates=(AggregateSpec("count", AggregateOperation.COUNT),),
        )
        self.plan = replace(self.plan, rules=(*self.plan.rules, rule))
        self.effective = mapping_source_selection(self.physical, self.plan)
        self.assertEqual(len(self.effective.datasets), 2)
        self.assertFalse(self.reader.supports(self.workspace_id, self.effective, structural_dataset_id(rule)))
        self.assertTrue(self.reader.supports(self.workspace_id, self.effective, self.dataset.dataset_id))
        self._assert_choices_and_coverage()
