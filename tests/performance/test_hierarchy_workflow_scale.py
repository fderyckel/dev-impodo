"""Opt-in measurements of accepted files on the generated-hierarchy route."""

import json
import os
import unittest
from pathlib import Path
from time import perf_counter

import psutil

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from scripts.benchmark_dependency_execution import _PeakSampler
from tests.support.hierarchy_workflow import PRODUCT_ROWS, HierarchyWorkflowFixture


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_HIERARCHY_SCALE") == "1",
    "Generated-hierarchy scale qualification is opt-in",
)
class HierarchyWorkflowScaleTests(HierarchyWorkflowFixture):
    def test_generated_hierarchy_preserves_every_source_row_at_its_route_limit(self):
        measurements = []
        for count, blank_every in ((1000, 0), (10000, 0), (25000, 0), (10000, 5)):
            rows = tuple(
                (
                    ""
                    if blank_every and index % blank_every == 0
                    else f"SCALE{index:06d}",
                    *PRODUCT_ROWS[index % len(PRODUCT_ROWS)][1:],
                )
                for index in range(count)
            )
            workspace_id, _revision = self.hierarchy(rows=rows)
            started = perf_counter()
            with (
                _PeakSampler(psutil.Process()) as memory,
                collect_duckdb_request_timings() as timings,
            ):
                staging, quality = self.prepare(workspace_id)
            quarantined = count // blank_every if blank_every else 0
            self.assertEqual(quality.ready_count, count + 12 - quarantined)
            self.assertEqual(quality.quarantined_count, quarantined)
            self.assertEqual(quality.blocked_count, 0)
            self.assertEqual(len(staging.rows), count + 12)
            self.assertEqual(timings.lock_retry_count, 0)
            measurements.append(
                {
                    "physical_rows": count,
                    "prepared_records": count + 12,
                    "quarantined_records": quarantined,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "peak_process_rss_bytes": memory.peak_bytes,
                    "connections": timings.connection_count,
                    "schema_checks": timings.schema_check_count,
                    "lock_retries": timings.lock_retry_count,
                }
            )
        output = os.environ.get("IMPODO_HIERARCHY_SCALE_REPORT")
        if output:
            Path(output).write_text(
                json.dumps(measurements, indent=2) + "\n", encoding="utf-8"
            )
