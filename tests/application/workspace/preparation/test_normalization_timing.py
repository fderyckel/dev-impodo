"""Keep optional bounded normalization timings separate from governed evidence."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from impodo.application.workspace.preparation import bounded_normalization
from impodo.application.workspace.preparation import normalization_service
from impodo.application.workspace.preparation.bounded_quality import (
    build_bounded_quality_run,
)
from impodo.application.workspace.preparation.normalization_service import (
    NormalizationService,
)
from impodo.domain.mapping.contracts import DatasetMapping, ScalarFieldMapping
from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.shared.access import LOCAL_ACTOR
from tests.domain.preparation.test_quality import (
    MAPPING_HASH,
    SCHEMA_HASH,
    SOURCE_HASH,
    _canonical_row,
    _staging,
    _stored_staging,
    _workspace_state,
)


class _ImpactRows:
    normalization_run_id = "staging:1"

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure

    def __iter__(self):
        return iter(())

    def prepare_normalization_facts(self, **_builders):
        if self.failure is not None:
            raise self.failure
        return {
            "effect_count": 0,
            "changed_record_count": 0,
            "examples": {},
            "effect_groups": (),
            "finding_groups": (),
        }

    def iter_normalization_effects(self):
        return iter(())

    def copy_normalization_effects(self, _connection, _run_id):
        return 0

    def iter_normalization_effect_json_batches(self, _connection, _batch_size):
        return iter(())


class NormalizationTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = _workspace_state()
        staging = _staging(self.workspace.workspace_id, (_canonical_row("a", 2),))
        ruleset = default_quality_ruleset(
            workspace_id=self.workspace.workspace_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        quality = evaluate_quality(
            workspace_state=self.workspace,
            staging=staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )
        self.staging = _stored_staging(staging)
        self.quality = build_bounded_quality_run(
            workspace_state=self.workspace,
            staging=self.staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
            published_staging_content_hash=staging.content_hash,
        ).with_content_hash(quality.content_hash)
        self.mapping = DatasetMapping(
            dataset_id="dataset:contacts",
            target_model="res.partner",
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    transform=ScalarTransformPolicy(trim=True),
                ),
            ),
        )
        self.arguments = {
            "workspace_state": self.workspace,
            "staging": self.staging,
            "quality": self.quality,
            "mappings": {"contacts": self.mapping},
            "impact_rows": _ImpactRows(),
            "staging_content_hash": staging.content_hash,
            "quality_content_hash": quality.content_hash,
            "effective": None,
        }

    def test_reports_completed_subphases_without_changing_evidence(self) -> None:
        expected = bounded_normalization.build_bounded_normalization_evaluation(
            **self.arguments
        )
        reporter = MagicMock()
        with patch(
            "impodo.application.workspace.preparation.timing.perf_counter",
            side_effect=(1.0, 1.25, 2.0, 2.5),
        ):
            actual = bounded_normalization.build_bounded_normalization_evaluation(
                **self.arguments, timing=reporter
            )
        self.assertEqual(
            [call.args for call in reporter.call_args_list],
            [
                ("normalization_effect_aggregation", 250.0, "completed"),
                ("normalization_eligible_hash", 500.0, "completed"),
            ],
        )
        self.assertEqual(replace(actual, effects=()), replace(expected, effects=()))
        self.assertEqual(tuple(actual.effects), tuple(expected.effects))

    def test_subphase_failures_propagate_without_claiming_later_work(self) -> None:
        for failing_stage in (
            "normalization_effect_aggregation",
            "normalization_eligible_hash",
        ):
            with self.subTest(failing_stage=failing_stage):
                failure = RuntimeError("Original operation failure")
                arguments = dict(self.arguments)
                if failing_stage == "normalization_effect_aggregation":
                    arguments["impact_rows"] = _ImpactRows(failure)
                reporter = MagicMock()
                with (
                    patch.object(
                        bounded_normalization,
                        "canonical_eligible_dataset_hash",
                        side_effect=failure,
                    ),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    bounded_normalization.build_bounded_normalization_evaluation(
                        **arguments, timing=reporter
                    )
                self.assertIs(raised.exception, failure)
                self.assertEqual(
                    [(call.args[0], call.args[2]) for call in reporter.call_args_list],
                    [
                        ("normalization_effect_aggregation", "failed"),
                    ]
                    if failing_stage == "normalization_effect_aggregation"
                    else [
                        ("normalization_effect_aggregation", "completed"),
                        ("normalization_eligible_hash", "failed"),
                    ],
                )

    def test_reporter_failure_preserves_evidence_and_original_operation_failure(
        self,
    ) -> None:
        expected = bounded_normalization.build_bounded_normalization_evaluation(
            **self.arguments
        )
        reporter = MagicMock(side_effect=OSError("Optional diagnostic failure"))
        actual = bounded_normalization.build_bounded_normalization_evaluation(
            **self.arguments, timing=reporter
        )
        self.assertEqual(replace(actual, effects=()), replace(expected, effects=()))
        self.assertEqual(tuple(actual.effects), tuple(expected.effects))
        self.assertEqual(reporter.call_count, 2)
        failure = RuntimeError("Original operation failure")
        arguments = {**self.arguments, "impact_rows": _ImpactRows(failure)}
        with self.assertRaises(RuntimeError) as raised:
            bounded_normalization.build_bounded_normalization_evaluation(
                **arguments, timing=reporter
            )
        self.assertIs(raised.exception, failure)

    def test_service_forwards_the_reporter_and_keeps_subphases_inside_aggregation(
        self,
    ) -> None:
        repository = MagicMock()
        service = NormalizationService(repository, MagicMock())
        revision = SimpleNamespace(definition=SimpleNamespace(datasets=(self.mapping,)))
        selection = SimpleNamespace(
            datasets=(SimpleNamespace(dataset_id="dataset:contacts", name="contacts"),)
        )
        reporter = MagicMock()
        with patch.object(
            normalization_service,
            "build_bounded_normalization_evaluation",
            wraps=bounded_normalization.build_bounded_normalization_evaluation,
        ) as build:
            result = service.evaluate_and_publish(
                self.workspace,
                revision,
                selection,
                self.staging,
                SimpleNamespace(
                    content_hash=self.arguments["staging_content_hash"],
                    run_id="staging:1",
                ),
                self.quality,
                SimpleNamespace(content_hash=self.quality.content_hash, run_id="quality:1"),
                _ImpactRows(),
                {"file:1": SOURCE_HASH},
                actor=LOCAL_ACTOR,
                timing=reporter,
            )
        self.assertIs(result, repository.publish_normalization_run.return_value)
        self.assertIs(build.call_args.kwargs["timing"], reporter)
        self.assertEqual(
            [(call.args[0], call.args[2]) for call in reporter.call_args_list],
            [
                ("normalization_effect_aggregation", "completed"),
                ("normalization_eligible_hash", "completed"),
                ("normalization_aggregation", "completed"),
                ("normalization_persistence_and_hash", "completed"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
