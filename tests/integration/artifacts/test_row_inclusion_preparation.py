"""Exercise admission, row review, quality and normalization for frozen filters."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4
import unittest

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.mapping.contracts import RowInclusionCondition, RowInclusionPolicy
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.performance import test_preparation_scale as fixtures
from tests.support.identity import resolve_local_actor


class RowInclusionPreparationTests(unittest.TestCase):
    setUp = fixtures.BoundedPreparationParityTests.setUp
    tearDown = fixtures.BoundedPreparationParityTests.tearDown

    def configure(self, count):
        original = fixtures.MappingDefinition

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
                                    "not_equals",
                                    "P000000",
                                ),
                            ),
                        ),
                        control_expectations=tuple(
                            replace(expectation, expected_total=str(count - 1))
                            for expectation in mapping.control_expectations
                        ),
                    )
                    for mapping in definition.datasets
                ),
            )

        with (
            patch.object(fixtures, "MappingDefinition", filtered),
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

    def assert_counts(self, count):
        service = self.context.preparation
        staging = service.staging.get_current_staging_summary(self.workspace_id)
        quality = service.quality.current_summary(self.workspace_id)
        normalization = service.normalization.current_summary(self.workspace_id)
        self.assertEqual(staging.total_rows, count)
        self.assertEqual(quality.ready_count, count - 1)
        self.assertEqual(quality.excluded_count, 1)
        self.assertEqual(quality.quarantined_count, 0)
        self.assertEqual(normalization.eligible_record_count, count - 1)
        self.assertEqual(normalization.set_aside_record_count, 1)

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
