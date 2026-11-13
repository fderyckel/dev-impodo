"""Observe evaluator fallbacks without exposing input or changing preparation."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from impodo.application.workspace.preparation.fallback_performance import (
    MAX_DATASET_ROUTE_BYTES,
    MAX_DATASET_ROUTE_OBSERVATIONS,
    MAX_DATASET_ROUTE_REASON_CODES,
    DatasetPreparationPerformance,
)
from impodo.application.workspace.preparation.preparation_capability import (
    PreparationCapabilityManifest,
    PreparationDatasetCapability,
    PreparationRouteBehavior,
    PreparationStageCapability,
)
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

MAPPING_HASH = "sha256:" + "a" * 64


def _selection(counts=(3, 8)):
    return SourceSelection(
        selection_id="selection:1",
        version=1,
        data_version_id="version:1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="private operator",
        content_hash="sha256:" + "b" * 64,
        datasets=tuple(
            SourceDataset(
                dataset_id=f"dataset:{index}",
                name=f"private customer table {index}",
                source=FileSourceBinding(
                    file_id=f"source:{index}",
                    table_key="csv",
                    source_sha256="sha256:" + "c" * 64,
                    catalog_hash="sha256:" + "d" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                ),
                row_count=count,
                columns=(SourceDatasetColumn(1, "private column", "column:1", "text"),),
            )
            for index, count in enumerate(counts)
        ),
    )


def _capability(selection, *, reasons=("COLUMNAR_FORMULA_UNSUPPORTED",)):
    return PreparationCapabilityManifest(
        physical_rows=sum(item.row_count for item in selection.datasets),
        supported_rows=50_000,
        stages=(
            PreparationStageCapability(
                "transformation",
                PreparationRouteBehavior.BOUNDED_PYTHON,
                50_000,
            ),
        ),
        datasets=tuple(
            PreparationDatasetCapability(
                dataset.dataset_id,
                dataset.name,
                PreparationRouteBehavior.BOUNDED_PYTHON,
                reason_codes=reasons,
                rule_paths=("/fields/private column/formula",),
            )
            for dataset in selection.datasets
        ),
    )


class DatasetRoutePerformanceTests(unittest.TestCase):
    def observer(self, *, selection=None, reasons=None, sink=None, clock=None):
        selection = selection or _selection()
        records = []
        kwargs = {} if clock is None else {"clock": clock}
        observer = DatasetPreparationPerformance(
            _capability(selection, **({} if reasons is None else {"reasons": reasons})),
            selection,
            mapping_hash=MAPPING_HASH,
            source_selection_hash=selection.content_hash,
            sink=records.append if sink is None else sink,
            **kwargs,
        )
        return observer, records

    def test_actual_native_evaluation_and_python_adaptation_are_distinct(self):
        now = [10.0]
        observer, records = self.observer(clock=lambda: now[0])
        observer.publish()
        initial = records[-1]["workload"]["dataset_routes"]
        self.assertTrue(all(item["actual_route"] == "UNOBSERVED" for item in initial))
        observer.start("dataset:0", "NATIVE_COLUMNAR")
        observer.adaptation("dataset:0", "PYTHON_ROW_ADAPTATION")
        now[0] = 11.25
        observer.finish("dataset:0")
        observer.finish("dataset:0")
        final = records[-1]["workload"]
        first, second = final["dataset_routes"]
        self.assertEqual(first["predicted_route"], "BOUNDED_PYTHON")
        self.assertEqual(first["actual_route"], "NATIVE_COLUMNAR")
        self.assertEqual(first["adaptation_route"], "PYTHON_ROW_ADAPTATION")
        self.assertEqual(first["outcome"], "SUCCEEDED")
        self.assertEqual(first["duration_ms"], 1250)
        self.assertEqual(second["outcome"], "PREDICTED")
        self.assertEqual(final["dataset_route_coverage"]["row_count"], 11)
        self.assertEqual(sum(item["row_count"] for item in final["dataset_routes"]), 11)
        self.assertEqual(len(records), 3)
        self.assertEqual(initial[0]["actual_route"], "UNOBSERVED")

    def test_multiple_reasons_do_not_multiply_rows_or_expose_business_values(self):
        observer, records = self.observer(
            reasons=(
                "COLUMNAR_FORMULA_UNSUPPORTED",
                "COLUMNAR_FORMULA_UNSUPPORTED",
                "COLUMNAR_PATTERN_REPLACEMENT_UNSUPPORTED",
                "private customer value",
            )
        )
        observer.publish()
        workload = records[-1]["workload"]
        self.assertEqual(workload["dataset_route_coverage"]["row_count"], 11)
        self.assertEqual(workload["dataset_routes"][0]["omitted_reason_count"], 1)
        self.assertEqual(len(workload["dataset_routes"][0]["reason_codes"]), 2)
        encoded = canonical_json_bytes(records).decode()
        for private in ("private", "dataset:0", "column:1", "source:0", "/fields"):
            self.assertNotIn(private, encoded)

    def test_entry_byte_and_reason_caps_preserve_explicit_coverage(self):
        from impodo.application.workspace.preparation.fallback_performance import (
            FALLBACK_REASON_CODES,
        )

        selection = _selection(tuple(range(1, 257)))
        observer, records = self.observer(
            selection=selection,
            reasons=tuple(sorted(FALLBACK_REASON_CODES)),
        )
        observer.publish()
        for dataset in selection.datasets:
            observer.start(dataset.dataset_id, "NATIVE_COLUMNAR")
            observer.adaptation(dataset.dataset_id, "PYTHON_ROW_ADAPTATION")
            observer.finish(dataset.dataset_id)
        for record in records:
            workload = record["workload"]
            observations = workload["dataset_routes"]
            coverage = workload["dataset_route_coverage"]
            self.assertLessEqual(
                len(canonical_json_bytes(workload)), MAX_DATASET_ROUTE_BYTES
            )
            self.assertLessEqual(len(observations), MAX_DATASET_ROUTE_OBSERVATIONS)
            self.assertTrue(
                all(
                    len(item["reason_codes"]) <= MAX_DATASET_ROUTE_REASON_CODES
                    for item in observations
                )
            )
            self.assertEqual(
                coverage["captured_dataset_count"] + coverage["omitted_dataset_count"],
                256,
            )
            self.assertEqual(
                coverage["captured_row_count"] + coverage["omitted_row_count"],
                sum(range(1, 257)),
            )
            self.assertEqual(
                sum(item["row_count"] for item in observations),
                coverage["captured_row_count"],
            )
            self.assertGreater(coverage["omitted_dataset_count"], 0)

    def test_interrupted_dataset_retains_partial_time_without_claiming_cancellation(
        self,
    ):
        now = [5.0]
        observer, records = self.observer(clock=lambda: now[0])
        observer.start("dataset:0", "BOUNDED_PYTHON")
        observer.adaptation("dataset:0", "PYTHON_RECORDS")
        now[0] = 5.75
        observer.interrupt_active()
        first, second = records[-1]["workload"]["dataset_routes"]
        self.assertEqual(first["outcome"], "INTERRUPTED")
        self.assertEqual(first["duration_ms"], 750)
        self.assertEqual(second["outcome"], "PREDICTED")

    def test_materialized_completion_does_not_invent_individual_dataset_timings(self):
        observer, records = self.observer()
        observer.complete_materialized()
        for item in records[-1]["workload"]["dataset_routes"]:
            self.assertEqual(item["actual_route"], "MATERIALIZING")
            self.assertEqual(item["adaptation_route"], "MATERIALIZED_RECORDS")
            self.assertEqual(item["outcome"], "SUCCEEDED")
            self.assertNotIn("duration_ms", item)

    def test_optional_sink_and_clock_failures_cannot_change_evaluator_work(self):
        def broken(*_args):
            raise RuntimeError("optional observation failure")

        observer, _records = self.observer(sink=broken, clock=broken)
        observer.publish()
        observer.start("dataset:0", "BOUNDED_PYTHON")
        observer.adaptation("dataset:0", "PYTHON_RECORDS")
        observer.finish("dataset:0")
        observer.interrupt_active()
        observer.complete_materialized()

    def test_metadata_observation_uses_no_artifact_reads_or_recompilation(self):
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected file read")),
            patch(
                "impodo.domain.compiler.columnar_transformation.compile_columnar_transformation_programs",
                side_effect=AssertionError("unexpected recompilation"),
            ),
        ):
            observer, records = self.observer()
            observer.publish()
            observer.start("dataset:0", "BOUNDED_PYTHON")
            observer.finish("dataset:0")
        self.assertEqual(
            records[-1]["workload"]["dataset_routes"][0]["outcome"], "SUCCEEDED"
        )


if __name__ == "__main__":
    unittest.main()
