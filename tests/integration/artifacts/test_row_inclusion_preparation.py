"""Exercise admission, row review, quality and normalization for frozen filters."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import unittest

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb import preparation_quality_index as quality_index
from impodo.domain.mapping.contracts import RowInclusionCondition, RowInclusionPolicy
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.performance import test_preparation_scale as fixtures
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class RowInclusionPreparationTests(unittest.TestCase):
    setUp = fixtures.BoundedPreparationParityTests.setUp
    tearDown = fixtures.BoundedPreparationParityTests.tearDown

    def configure(self, count, *, exclude_most=False, major=19, mode=OdooConnectionMode.LOCAL):
        original = fixtures.MappingDefinition
        original_catalog = fixtures.OdooSchemaCatalog
        original_target = self.context.workspace_states.update_target
        included_count = 1 if exclude_most else count - 1

        def catalog(*args, **kwargs):
            return replace(original_catalog(*args, **kwargs), odoo_version=f"{major}.0")

        def target(*args, **kwargs):
            kwargs["odoo_connection_mode"] = mode.value
            if mode is OdooConnectionMode.REMOTE:
                kwargs["odoo_base_url"] = "https://odoo.example.test"
            return original_target(*args, **kwargs)

        def filtered(*args, **kwargs):
            definition = original(*args, **kwargs)
            return replace(
                definition,
                datasets=tuple(
                    replace(
                        mapping,
                        row_inclusion=RowInclusionPolicy(
                            mode="matching_rows",
                            conditions=(
                                RowInclusionCondition(
                                    str(uuid4()),
                                    mapping.source_identity_column_keys[0],
                                    "equals" if exclude_most else "not_equals",
                                    "P000000",
                                ),
                            ),
                        ),
                        control_expectations=tuple(
                            replace(expectation, expected_total=str(included_count))
                            for expectation in mapping.control_expectations
                        ),
                    )
                    for mapping in definition.datasets
                ),
            )

        with (
            patch.object(fixtures, "MappingDefinition", filtered),
            patch.object(fixtures, "OdooSchemaCatalog", catalog),
            patch.object(self.context.workspace_states, "update_target", target),
            patch.object(fixtures, "PREPARATION_SCALE_WORKLOAD", "products"),
            patch.object(fixtures, "PREPARATION_SCALE_EFFECT_FIELDS", 1),
        ):
            self.workspace_id, _, _ = (
                fixtures.PreparationWorkflowScaleTests._prepare_project_and_evidence(
                    self,
                    row_count=count,
                    column_count=6,
                    mapped_field_count=5,
                )
            )
        self.actor = resolve_local_actor(self.context)
        revision = self.context.preparation.mappings.get_mapping_revision(
            self.workspace_id
        )
        self.review = run_local_operation_sync(
            self.context.row_inclusion_reviews.check_current,
            self.workspace_id,
            actor=self.actor,
        )
        run_local_operation_sync(
            self.context.row_inclusion_reviews.confirm_current,
            self.workspace_id,
            datasets=revision.definition.datasets,
            expected_revision_version=revision.version,
            expected_working_draft_version=1,
            actor=self.actor,
        )

    def prepare(self, *, progress=None):
        with (
            patch.object(
                fixtures.preparation_module,
                "stage_browser_mapping",
                side_effect=AssertionError("whole-run source fallback"),
            ),
            patch.object(
                fixtures.quality_module,
                "evaluate_quality",
                side_effect=AssertionError("whole-run quality fallback"),
            ),
            patch.object(
                fixtures.normalization_module,
                "evaluate_normalization",
                side_effect=AssertionError("whole-run normalization fallback"),
            ),
        ):
            return self.context.preparation.prepare(self.workspace_id, actor=self.actor, progress=progress)

    def assert_counts(self, count, *, included_count=None):
        included_count = count - 1 if included_count is None else included_count
        service = self.context.preparation
        staging = service.staging.get_current_staging_summary(self.workspace_id)
        quality = service.quality.current_summary(self.workspace_id)
        normalization = service.normalization.current_summary(self.workspace_id)
        self.assertEqual(staging.total_rows, count)
        self.assertEqual(quality.ready_count, included_count)
        self.assertEqual(quality.excluded_count, count - included_count)
        self.assertEqual(quality.quarantined_count, 0)
        self.assertEqual(normalization.eligible_record_count, included_count)
        self.assertEqual(normalization.set_aside_record_count, count - included_count)

    def assert_majority_excluded(self, major, mode):
        self.configure(65, exclude_most=True, major=major, mode=mode)
        with (
            patch("impodo.adapters.duckdb.preparation_quality_index.QUALITY_FINDING_INDEX_MAX_BYTES", 32 * 1024),
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as sql,
        ):
            self.prepare()
        self.assert_counts(65, included_count=1)
        run = self.context.preparation.quality.current_run(self.workspace_id)
        self.assertEqual(run.issues, ())
        self.assertEqual(run.quarantine, ())
        self.assertEqual(len(run.row_results), 65)
        self.assertEqual(len(run.source_accounting), 65)
        self.assertEqual(len(run.eligible_row_ids), 1)
        original_projection = quality_index._read_finding_projection

        def full_projection(cursor, *, budget=None, **_kwargs):
            return original_projection(cursor, budget=budget)

        with patch.object(quality_index, "_read_finding_projection", full_projection):
            self.prepare()
        expected = self.context.preparation.quality.current_run(self.workspace_id)
        self.assertEqual(run.to_json(), expected.to_json())
        self.assertEqual(run.content_hash, expected.content_hash)
        self.assertEqual(self.review.included_count, 1)
        self.assertLessEqual(timing.connection_count, 40)
        self.assertLessEqual(timing.schema_check_count, 28)
        self.assertLessEqual(len(sql), 558)
        self.assertIsNone(getattr(_read_databases, "connections", None))

    def test_majority_excluded_odoo19_local(self):
        self.assert_majority_excluded(19, OdooConnectionMode.LOCAL)

    def test_majority_excluded_odoo19_remote(self):
        self.assert_majority_excluded(19, OdooConnectionMode.REMOTE)

    def test_majority_excluded_odoo20_local(self):
        self.assert_majority_excluded(20, OdooConnectionMode.LOCAL)

    def test_majority_excluded_odoo20_remote(self):
        self.assert_majority_excluded(20, OdooConnectionMode.REMOTE)

    def test_full_preparation_admits_row_rules_above_legacy_python_limit(self):
        self.configure(17)
        with patch.object(
            fixtures.bounded_preparation_module,
            "BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT",
            16,
        ):
            self.prepare()
        self.assert_counts(17)

    def test_phase_boundaries_and_failed_retry_release_database_owners(self):
        self.configure(17)
        phases = []

        def progress(phase, _completed, _total, _message):
            if not phases or phases[-1] != phase:
                self.assertIsNone(getattr(_read_databases, "connections", None))
                phases.append(phase)

        with patch.object(self.context.preparation.quality, "evaluate_and_publish", side_effect=RuntimeError("controlled quality failure")):
            with self.assertRaisesRegex(RuntimeError, "controlled quality failure"):
                self.prepare(progress=progress)
        self.assertIsNone(getattr(_read_databases, "connections", None))
        phases.clear()
        self.prepare(progress=progress)
        self.assertIsNone(getattr(_read_databases, "connections", None))
        self.assertEqual(phases[-1].value, "COMPLETE")
        self.assert_counts(17)


if __name__ == "__main__":
    unittest.main()
