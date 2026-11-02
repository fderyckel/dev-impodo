"""Joined Rows to use reviews read bounded logical outputs with exact evidence."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.mapping.bounded_join_review import joined_row_inclusion_review, joined_transformation_impact
from impodo.application.workspace.mapping.row_inclusion_review import RowInclusionReviewService
from impodo.application.workspace.mapping.transformation_impact import TransformationImpactService
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping, MappingDefinition, RowInclusionCondition, RowInclusionPolicy, ScalarFieldMapping
from impodo.domain.mapping.row_inclusion_review import RowInclusionReviewOutcome
from impodo.domain.preparation.source import SourceLoadError, SourceRow, SourceTable
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode, WorkspaceState
from tests.integration.artifacts import test_joined_mapping_values as fixtures


class JoinedRowInclusionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.JoinedMappingValueTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def configure(self, *, large=False, condition_type="string"):
        f = self.fixture
        if large:
            f._prepare_join(row_counts=(4_753, 44_387, 16_159), wide=False)
        self.workspace = WorkspaceState(f.workspace_id, "Generic joined review", "CSV")
        identity = next(x.stable_key for x in f.dataset.columns if x.source_name == "ID")
        self.definition = MappingDefinition(str(uuid4()), f.effective.content_hash, "sha256:" + "9" * 64, (
            DatasetMapping(f.dataset.dataset_id, "x_generic.record",
                source_identity_column_keys=(identity,),
                target_identity=(IdentityComponentMapping((identity,), ("external_key",), "integer"),),
                row_inclusion=RowInclusionPolicy(
                mode="matching_rows", conditions=(RowInclusionCondition(
                    str(uuid4()), f.key, "equals", "value-1" if large else "BE", condition_type,
                ),),
            )),
        ))
        self.context = SimpleNamespace(
            workspace_state=self.workspace, revision=SimpleNamespace(definition=self.definition, version=1),
            physical_selection=f.physical, effective_selection=f.effective, plan=f.plan,
        )
        f.sources.get_source_catalogs = lambda _: ()
        checked = SimpleNamespace(
            context=lambda _: self.context, sources=f.sources,
            mappings=SimpleNamespace(get_mapping_working_draft=lambda _: None),
            artifacts=f.artifacts, grouped_exact_joins=f.executor,
        )
        self.reviews = Mock()
        self.reviews.replace_current_review.side_effect = lambda _workspace, report, **kwargs: report
        self.service = RowInclusionReviewService(checked, self.reviews, Mock())

    def test_65299_inputs_review_only_4753_joined_rows_and_preview_does_not_publish(self):
        self.configure(large=True)
        f = self.fixture
        self.assertEqual(sum(x.row_count for x in f.physical.datasets), 65_299)
        from impodo.adapters.duckdb import grouped_exact_join_source as reader

        with (
            patch("impodo.application.workspace.mapping.row_inclusion_review.stage_browser_mapping", side_effect=AssertionError("materialized evaluator used")),
            patch.object(reader, "_create_narrow_input", wraps=reader._create_narrow_input) as scan,
            collect_duckdb_request_timings() as timings,
        ):
            report = self.service.preview_definition(f.workspace_id, self.definition, actor=LOCAL_ACTOR)
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(timings.lock_retry_count, 0)
        self.assertEqual(scan.call_count, 3)
        self.assertEqual([x.kwargs["needed_column_keys"] for x in scan.call_args_list], [("key",), ("key", "region"), ("key",)])
        self.assertEqual(len(report.rows), 4_753)
        self.assertEqual(report.datasets[0].included_count, 951)
        self.assertEqual(report.datasets[0].excluded_count, 3_802)
        self.assertEqual(report.identity.derived_plan_hash, f.plan.content_hash)
        self.assertEqual(report.identity.physical_selection_hash, f.physical.content_hash)
        self.assertEqual([x.source_row for x in report.rows], list(range(1, 4_754)))
        self.reviews.replace_current_review.assert_not_called()
        operation_id = str(uuid4())
        checked = self.service.check_current(f.workspace_id, actor=LOCAL_ACTOR, operation_id=operation_id)
        self.assertEqual(checked, report)
        self.assertEqual(self.reviews.replace_current_review.call_args.kwargs["operation_id"], operation_id)

    def test_small_join_matches_materialized_oracle_including_row_types_and_order(self):
        self.configure()
        f = self.fixture
        self.definition = replace(self.definition, datasets=(replace(
            self.definition.datasets[0], fields=(ScalarFieldMapping(
                "choice", f.key, transform=ScalarTransformPolicy(case_mode="lowercase"),
            ),),
        ),))
        values = {
            "primary": ((1, "Alpha"), (2, "Beta"), (3, "Gamma")),
            "regions": ((1, "BE"), (2, "FR"), (3, "NL")),
            "emails": ((1, "alpha@example.test"), (2, "beta@example.test"), (3, "gamma@example.test")),
        }
        tables = {
            dataset.dataset_id: SourceTable(
                dataset.name, Path(f"{dataset.name}.csv"), tuple(c.source_name for c in dataset.columns),
                tuple(SourceRow(i, dict(zip((c.source_name for c in dataset.columns), row, strict=True)))
                      for i, row in enumerate(values[dataset.name], 1)),
                dataset.source_evidence_hash,
            )
            for dataset in f.physical.datasets
        }
        oracle_impacts = []
        oracle = evaluate_browser_mapping(
            workspace_id=f.workspace_id, definition=self.definition, physical_selection=f.physical,
            effective_selection=f.effective, plan=f.plan, loaded_tables=tables,
            collect_transformation_impact=True, transformation_detail_limit=0,
            transformation_impact_sink=oracle_impacts.append,
        )
        report = self.service.preview_definition(f.workspace_id, self.definition, actor=LOCAL_ACTOR)
        self.assertEqual(report, oracle.row_inclusion_review)
        impacts = []
        effects = joined_transformation_impact(self.workspace, self.definition, f.physical,
                                             f.effective, f.plan, f.artifacts, f.executor, f.snapshots, impacts.append)
        self.assertEqual(effects, oracle.transformation_impact)
        self.assertEqual(effects.changed_count, 1)
        self.assertEqual(impacts, oracle_impacts)

    def test_large_join_rule_effects_use_logical_reader_and_stream_included_rows(self):
        self.configure(large=True)
        f = self.fixture
        self.definition = replace(self.definition, datasets=(replace(
            self.definition.datasets[0], fields=(ScalarFieldMapping(
                "choice", f.key, transform=ScalarTransformPolicy(case_mode="uppercase"),
            ),),
        ),))
        self.context.revision.definition = self.definition
        self.context.identity = object()
        service = TransformationImpactService(Mock(), Mock(), f.sources, f.derivations,
                                              Mock(), f.artifacts, Mock(), grouped_exact_joins=f.executor)
        impacts = []
        with (
            patch.object(service, "context", return_value=self.context),
            patch.object(service, "replace_snapshot", side_effect=lambda _workspace, _identity, build, **kwargs: build(impacts.append)),
            patch("impodo.application.workspace.mapping.transformation_impact.stage_browser_mapping", side_effect=AssertionError("materialized evaluator used")),
            collect_duckdb_request_timings() as timings,
        ):
            report = service.prepare_snapshot(f.workspace_id, actor=LOCAL_ACTOR)
        self.assertEqual(report.changed_count, 951)
        self.assertEqual(len(impacts), 951)
        self.assertEqual(report.rows, ())
        self.assertEqual(timings.connection_count, 1)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(timings.lock_retry_count, 0)

    def test_local_remote_odoo_19_and_20_snapshots_use_the_same_generic_reader(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.fixture._prepare_join(odoo_major=major)
                    self.configure()
                    self.context.workspace_state = replace(self.workspace, source_mode=SourceMode.ODOO, odoo_connection_mode=mode)
                    report = self.service.check_current(self.fixture.workspace_id, actor=LOCAL_ACTOR)
                    self.assertEqual(len(report.rows), 3)
                    self.assertEqual(report.datasets[0].included_count, 1)
                    definition = replace(self.definition, datasets=(replace(
                        self.definition.datasets[0], fields=(ScalarFieldMapping(
                            "choice", self.fixture.key,
                            transform=ScalarTransformPolicy(case_mode="lowercase"),
                        ),),
                    ),))
                    impacts = []
                    effects = joined_transformation_impact(
                        self.context.workspace_state, definition, self.fixture.physical,
                        self.fixture.effective, self.fixture.plan, self.fixture.artifacts,
                        self.fixture.executor, self.fixture.snapshots, impacts.append,
                    )
                    self.assertEqual(effects.changed_count, 1)
                    self.assertEqual(len(impacts), 1)

    def test_failure_after_reading_rows_does_not_publish_partial_evidence(self):
        self.configure()
        f = self.fixture
        original = f.executor.iter_batches

        def failing(**kwargs):
            yield from original(**kwargs)
            raise SourceLoadError("read interrupted after a batch")

        with patch.object(f.executor, "iter_batches", side_effect=failing), self.assertRaises(ReadinessError):
            self.service.check_current(f.workspace_id, actor=LOCAL_ACTOR)
        self.reviews.replace_current_review.assert_not_called()

    def test_stale_snapshot_review_and_wrong_workspace_fail_without_publication(self):
        self.configure()
        f = self.fixture
        for plan in (
            replace(f.plan, workspace_id=str(uuid4())),
            replace(f.plan, join_reviews=(replace(f.plan.join_reviews[0], input_snapshots=()), *f.plan.join_reviews[1:])),
        ):
            with self.subTest(workspace=plan.workspace_id):
                self.context.plan = plan
                with self.assertRaises(ReadinessError), patch.object(f.executor, "iter_batches", side_effect=AssertionError("opened stale inputs")):
                    self.service.check_current(f.workspace_id, actor=LOCAL_ACTOR)
                self.reviews.replace_current_review.assert_not_called()

    def test_bad_condition_values_are_reviewable_and_do_not_approve_rows(self):
        self.configure()
        f = self.fixture
        mapping = self.definition.datasets[0]
        self.definition = replace(self.definition, datasets=(replace(mapping, row_inclusion=replace(
            mapping.row_inclusion, conditions=(replace(mapping.row_inclusion.conditions[0], comparison_value="1", value_type="integer"),),
        )),))
        report = self.service.preview_definition(f.workspace_id, self.definition, actor=LOCAL_ACTOR)
        self.assertEqual(report.datasets[0].cannot_evaluate_count, 3)
        self.assertFalse(report.datasets[0].confirmable)
        self.assertTrue(all(x.outcome is RowInclusionReviewOutcome.CANNOT_EVALUATE for x in report.rows))

    def test_review_output_limit_is_checked_before_opening_snapshots(self):
        self.configure()
        f = self.fixture
        effective = replace(f.effective, datasets=(replace(f.dataset, row_count=50_001),))
        with self.assertRaisesRegex(ReadinessError, "logical output rows"), patch.object(f.executor, "iter_batches", side_effect=AssertionError("opened over-limit outputs")):
            joined_row_inclusion_review(self.workspace, self.definition, f.physical, effective,
                                       f.plan, f.artifacts, f.executor, f.snapshots)
