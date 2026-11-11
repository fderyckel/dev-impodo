"""Verify saved pairwise joins across choices, reviews, and preparation."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.mapping_source_values import JoinedMappingSourceValues
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.data_version.source_snapshots import load_source_snapshot_table
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.workspace.mapping.bounded_join_review import (
    joined_row_inclusion_review,
)
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    bounded_exact_join_group,
    prepare_bounded_grouped_exact_join_session,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    ScalarFieldMapping,
    SelectionConditionOperator,
    ValueMapping,
)
from impodo.domain.serialization import content_hash
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    ExactJoinInputSnapshotEvidence,
    SavedExactJoinReview,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    mapping_source_selection,
)
from impodo.domain.workspace.errors import SourcePreparationReadError, WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus
from tests.integration.artifacts.test_grouped_exact_join_preparation import (
    _publish_snapshot,
    _selection,
)
from tests.support.database_probe import count_duckdb_statements


class StandaloneJoinValueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = DuckDbWorkspaceDatabase(root / "databases")
        self.artifacts = LocalArtifactStore(root / "artifacts")
        self.workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Pairwise join",
            source_system="CSV",
            status=WorkspaceStatus.REGISTERED,
            registered_at=datetime.now(UTC),
        )
        WorkspaceStateRepository(self.database).initialize_workbench(
            self.workspace,
            actor=LOCAL_ACTOR,
        )
        self._prepare()

    def _prepare(self, *, kind=JoinKind.LEFT, unmatched=True, sibling=False):
        selection = _selection()
        primary_rows = ((1, "Alpha"), (1, "Another"), (2, "Beta"))
        if unmatched:
            primary_rows += ((4, "Unmatched"),)
        self.physical = replace(
            selection,
            datasets=(
                replace(selection.datasets[0], row_count=len(primary_rows)),
                selection.datasets[1],
                *((selection.datasets[2],) if sibling else ()),
            ),
        )
        primary, right = self.physical.datasets[:2]
        self.snapshots = tuple(
            _publish_snapshot(
                self.artifacts,
                self.physical,
                dataset,
                rows=primary_rows if dataset.dataset_id == primary.dataset_id else None,
            )
            for dataset in self.physical.datasets
        )
        self.rule = ExactJoinRule(
            rule_id=str(uuid4()),
            output_dataset_name="records_combined",
            left_dataset_id=primary.dataset_id,
            right_dataset_id=right.dataset_id,
            keys=(JoinKey("key", "key", "integer"),),
            kind=kind,
            require_all_right_rows=False,
            output_columns=(
                StructuralOutputColumn("id", "ID", "integer"),
                StructuralOutputColumn("name", "Name", "string"),
                StructuralOutputColumn("choice", "Choice", "string"),
            ),
            projections=(
                StructuralProjection("id", primary.dataset_id, "key"),
                StructuralProjection("name", primary.dataset_id, "name"),
                StructuralProjection("choice", right.dataset_id, "region"),
            ),
        )
        with ExitStack() as stack:
            tables = {}
            for snapshot in self.snapshots:
                path = stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        self.physical.data_version_id,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
                )
                tables[snapshot.dataset_id] = load_source_snapshot_table(path, snapshot)
            review = DerivedEntityWorkspaceService._review_join(
                self.rule,
                self.physical,
                None,
                tables,
            )
        self.assertTrue(review.can_save)
        self.plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=self.workspace.workspace_id,
            source_selection_hash=self.physical.content_hash,
            rules=(self.rule,),
            updated_at=datetime.now(UTC),
            updated_by="Test operator",
            dataset_uses=tuple(
                SourceDatasetUsePolicy(
                    dataset.dataset_id,
                    SourceDatasetUse.MAP_DIRECTLY
                    if sibling and dataset == self.physical.datasets[-1]
                    else SourceDatasetUse.PREPARATION_ONLY,
                )
                for dataset in self.physical.datasets
            ),
            join_reviews=(
                SavedExactJoinReview(
                    self.rule.rule_id,
                    content_hash(self.rule.to_dict()),
                    self.physical.content_hash,
                    tuple(
                        ExactJoinInputSnapshotEvidence(
                            snapshot.dataset_id,
                            snapshot.logical_hash,
                            snapshot.parquet_sha256,
                        )
                        for snapshot in self.snapshots
                    ),
                    review,
                ),
            ),
        )
        self.plan = DerivedEntityPlan.from_json(self.plan.to_json())
        self.effective = mapping_source_selection(self.physical, self.plan)
        self.dataset = next(
            item for item in self.effective.datasets if item.name == "records_combined"
        )
        self.executor = DuckDbGroupedExactJoinSource(self.database)
        self.reader = JoinedMappingSourceValues(
            SimpleNamespace(
                get_source_selection=lambda _: self.physical,
                get_mapping_source_selection=lambda _: self.effective,
                get_current_source_snapshots=lambda _: self.snapshots,
            ),
            SimpleNamespace(get_derived_entity_plan=lambda _: self.plan),
            self.artifacts,
            self.executor,
        )
        self.coverage = CategoricalCoverageService(
            self.reader.sources, self.artifacts, self.reader
        )

    def _definition(self, *, policy=RowInclusionPolicy()):
        return MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=self.effective.content_hash,
            schema_hash="sha256:" + "9" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=self.dataset.dataset_id,
                    target_model="x_generic.record",
                    source_identity_column_keys=("name",),
                    target_identity=(
                        IdentityComponentMapping(
                            ("name",), ("external_key",), "string"
                        ),
                    ),
                    fields=(ScalarFieldMapping("display_name", "name"),),
                    row_inclusion=policy,
                ),
            ),
        )

    def test_left_join_preserves_repeated_primary_rows_unmatched_blanks_and_identity(
        self,
    ):
        original = self.plan.to_json(), self.effective.content_hash
        self.assertIsNone(self.plan.rules[0].group_id)
        self.assertTrue(
            self.reader.supports(
                self.workspace.workspace_id, self.effective, self.dataset.dataset_id
            )
        )
        values = self.reader.project(
            self.workspace.workspace_id,
            self.effective,
            self.dataset.dataset_id,
            ("name", "choice"),
        )
        self.assertEqual(
            values.rows(),
            [("Alpha", "BE"), ("Another", "BE"), ("Beta", "FR"), ("Unmatched", None)],
        )
        self.assertEqual(
            self.coverage.source_value_choices(
                self.workspace.workspace_id, self.dataset.dataset_id, "choice"
            ),
            (
                {"value": "BE", "count": 2},
                {"value": "FR", "count": 1},
            ),
        )
        self.assertEqual((self.plan.to_json(), self.effective.content_hash), original)

    def test_inner_join_uses_inner_semantics_and_rejects_an_unmatched_saved_review(
        self,
    ):
        self._prepare(kind=JoinKind.INNER, unmatched=False)
        group = bounded_exact_join_group(
            self.physical, self.effective, self.plan, dataset_id=self.dataset.dataset_id
        )
        self.assertIsNotNone(group)
        self.assertEqual(
            self.reader.counts(
                self.workspace.workspace_id,
                self.effective,
                self.dataset.dataset_id,
                "choice",
                10,
            ),
            {"BE": 2, "FR": 1},
        )
        self._prepare()
        group = bounded_exact_join_group(
            self.physical, self.effective, self.plan, dataset_id=self.dataset.dataset_id
        )
        # Exercise the executor separately: an INNER step must exclude the
        # unmatched row; it cannot silently execute the LEFT SQL path.
        inner_rule = replace(self.rule, kind=JoinKind.INNER)
        inner = replace(
            group,
            rules=(inner_rule,),
            output_dataset_id=structural_dataset_id(inner_rule),
        )
        with ExitStack() as stack:
            paths = {
                snapshot.dataset_id: stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        self.physical.data_version_id,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
                )
                for snapshot in self.snapshots
            }
            values = self.executor.project_values(
                group=inner,
                physical_selection=self.physical,
                effective_dataset=replace(self.dataset, row_count=3),
                snapshots={item.dataset_id: item for item in self.snapshots},
                paths=paths,
                column_keys=("choice",),
            )
        self.assertEqual(values["choice"].to_list(), ["BE", "BE", "FR"])
        with self.assertRaisesRegex(ValueError, "successful exact-join review"):
            replace(
                self.plan.join_reviews[0],
                review=replace(
                    self.plan.join_reviews[0].review,
                    actual_output_rows=3,
                ),
            )

    def test_selected_join_ignores_sibling_output_and_keeps_reads_narrow(self):
        from impodo.adapters.duckdb import grouped_exact_join_source as module

        self._prepare(sibling=True)
        with (
            patch.object(
                module, "_create_narrow_input", wraps=module._create_narrow_input
            ) as scans,
            count_duckdb_statements() as statements,
            collect_duckdb_request_timings() as timings,
        ):
            counts = self.reader.counts(
                self.workspace.workspace_id,
                self.effective,
                self.dataset.dataset_id,
                "choice",
                1,
            )
        self.assertEqual(counts, {"BE": 2, "FR": 1})
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(
            [call.kwargs["needed_column_keys"] for call in scans.call_args_list],
            [("key",), ("key", "region")],
        )
        # Each input has CREATE + COUNT; one SELECT returns bounded choices.
        self.assertEqual(len(statements), 5)

    def test_stale_missing_and_unsupported_evidence_have_distinct_recovery(self):
        saved = self.plan
        cases = (
            (replace(saved, join_reviews=()), "SOURCE_PREPARATION_REVIEW_STALE"),
            (
                replace(
                    saved,
                    source_selection_hash="sha256:" + "a" * 64,
                    join_reviews=(
                        replace(
                            saved.join_reviews[0],
                            source_selection_hash="sha256:" + "a" * 64,
                        ),
                    ),
                ),
                "SOURCE_PREPARATION_REVIEW_STALE",
            ),
            (
                replace(saved, rules=(), join_reviews=()),
                "SOURCE_PREPARATION_UNSUPPORTED",
            ),
            (
                replace(
                    saved,
                    join_reviews=(replace(saved.join_reviews[0], input_snapshots=()),),
                ),
                "SOURCE_PREPARATION_REVIEW_STALE",
            ),
        )
        for plan, code in cases:
            with self.subTest(code=code, plan=plan.content_hash):
                self.plan = plan
                with self.assertRaises(SourcePreparationReadError) as failure:
                    self.reader.counts(
                        self.workspace.workspace_id,
                        self.effective,
                        self.dataset.dataset_id,
                        "choice",
                        10,
                    )
                self.assertEqual(failure.exception.failure_code, code)
                if code == "SOURCE_PREPARATION_UNSUPPORTED":
                    self.assertNotIn(
                        "Review the combined table", str(failure.exception)
                    )
        self.plan = saved
        self.snapshots = self.snapshots[:1]
        with self.assertRaises(SourcePreparationReadError) as failure:
            self.reader.counts(
                self.workspace.workspace_id,
                self.effective,
                self.dataset.dataset_id,
                "choice",
                10,
            )
        self.assertEqual(
            failure.exception.failure_code, "SOURCE_PREPARATION_EVIDENCE_INCOMPLETE"
        )

    def test_rows_to_use_and_preparation_share_the_saved_join_and_lineage(self):
        policy = RowInclusionPolicy(
            RowInclusionMode.MATCHING_ROWS,
            (
                RowInclusionCondition(
                    str(uuid4()),
                    "name",
                    SelectionConditionOperator.EQUALS,
                    "Alpha",
                    "string",
                ),
            ),
        )
        definition = self._definition(policy=policy)
        review = joined_row_inclusion_review(
            self.workspace,
            definition,
            self.physical,
            self.effective,
            self.plan,
            self.artifacts,
            self.executor,
            self.snapshots,
        )
        self.assertIsNotNone(review)
        self.assertEqual(
            (review.datasets[0].included_count, review.datasets[0].excluded_count),
            (1, 3),
        )
        sessions = PreparationSessionRepository(self.database, self.artifacts)
        prepared = prepare_bounded_grouped_exact_join_session(
            self.workspace,
            definition,
            1,
            self.physical,
            self.effective,
            self.plan,
            self.artifacts,
            None,
            sessions,
            self.executor,
            actor=LOCAL_ACTOR,
            source_snapshots=self.snapshots,
        )
        rows = tuple(prepared.run.rows)
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [row.disposition for row in rows],
            [StagingDisposition.CANDIDATE, *([StagingDisposition.EXCLUDED] * 3)],
        )
        self.assertEqual(len(rows[0].lineage.physical_sources), 2)
        self.assertEqual(rows[0].proposed_values["display_name"], "Alpha")
        self.assertEqual(prepared.run.derived_plan_hash, self.plan.content_hash)

    def test_relationship_coverage_is_generic_and_recipe_eligible_for_both_majors(self):
        for major in (19, 20):
            for model in ("res.partner", "product.template"):
                with self.subTest(major=major, model=model):
                    relation = RelationshipMapping(
                        target_field="x_link",
                        kind="many2one",
                        source_column_keys=("choice",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_CATALOG,
                            model="x_reference.list",
                            key_mappings=(ReferenceKeyMapping("choice", "code"),),
                            value_mappings=(
                                ValueMapping("BE", "Belgium"),
                                ValueMapping("FR", "France"),
                            ),
                        ),
                        categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
                    )
                    definition = self._definition()
                    definition = replace(
                        definition,
                        datasets=(
                            replace(
                                definition.datasets[0],
                                target_model=model,
                                relationships=(relation,),
                            ),
                        ),
                    )
                    schema = SimpleNamespace(
                        odoo_version=f"{major}.0",
                        models=(
                            SchemaModel(
                                model,
                                model,
                                (
                                    SchemaField(
                                        "x_link",
                                        "Linked list",
                                        "many2one",
                                        False,
                                        False,
                                        "x_reference.list",
                                        None,
                                        (),
                                    ),
                                ),
                            ),
                        ),
                    )
                    result = self.coverage.collect(
                        self.workspace.workspace_id, definition, self.effective, schema
                    )
                    self.assertEqual(result.issues, ())
                    self.assertTrue(result.evidence.recipe_eligible)
                    self.assertEqual(len(result.evidence.source_snapshot_hashes), 2)

    def test_recipe_publication_reviews_fresh_snapshots_and_rejects_missing_or_oversized_inputs(
        self,
    ):
        plans = Mock()
        plans.get_derived_entity_plan.return_value = None
        service = DerivedEntityWorkspaceService(
            self.reader.sources,
            plans,
            CapabilityAuthorizationPolicy(),
            self.artifacts,
        )
        unreviewed = replace(self.plan, join_reviews=())
        service.save_derived_entity_plan(
            self.workspace.workspace_id,
            unreviewed,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )
        published = plans.save_derived_entity_plan.call_args.args[1]
        self.assertEqual(published.rules, unreviewed.rules)
        self.assertEqual(published.join_reviews, self.plan.join_reviews)
        plans.save_derived_entity_plan.reset_mock()
        self.snapshots = ()
        with self.assertRaisesRegex(WorkspaceError, "Accept this Data version"):
            service.save_derived_entity_plan(
                self.workspace.workspace_id,
                unreviewed,
                expected_parent_version=None,
                actor=LOCAL_ACTOR,
            )
        plans.save_derived_entity_plan.assert_not_called()
        self.physical = replace(
            self.physical,
            datasets=tuple(
                replace(dataset, row_count=25_001) for dataset in self.physical.datasets
            ),
        )
        with (
            patch.object(service, "_load_join_tables") as load,
            self.assertRaisesRegex(WorkspaceError, "review limit"),
        ):
            service.save_derived_entity_plan(
                self.workspace.workspace_id,
                unreviewed,
                expected_parent_version=None,
                actor=LOCAL_ACTOR,
            )
        load.assert_not_called()
        plans.save_derived_entity_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
