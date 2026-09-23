"""Opt-in Windows qualification for local reviewed-scope calculation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter
import unittest

import psutil

from impodo.adapters.artifacts.reporting import (
    write_preflight_outputs,
    write_review_workbook,
)
from impodo.domain.preflight.deferred_scope import (
    DeferredDependencyFact,
    DeferredIssue,
    DeferredIssueScope,
    preview_deferred_scope,
)
from tests.integration.artifacts.test_preflight_outputs import golden_result


ROW_COUNT = int(os.environ.get("IMPODO_DEFERRED_SCOPE_ROWS", "25000"))
HASH = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class _Row:
    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    source_identity: tuple[object, ...]
    target_model: str
    disposition: str


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_DEFERRED_SCOPE_SCALE") == "1",
    "25,000-row deferred-scope qualification is opt-in",
)
class DeferredScopeScaleTests(unittest.TestCase):
    def test_relationship_closure_is_local_bounded_and_leaves_safe_remainder(self):
        self.assertGreaterEqual(ROW_COUNT, 2)
        affected_count = ROW_COUNT // 2
        rows = tuple(
            _Row(
                row_id="sha256:" + f"{index + 1:064x}",
                dataset="bom_lines" if index else "boms",
                source_row=index + 2,
                source_trace_id=f"trace-{index + 1}",
                source_identity=(f"ROW-{index + 1}",),
                target_model=("mrp.bom.line" if index else "mrp.bom"),
                disposition="BLOCKED" if index == 0 else "CREATE",
            )
            for index in range(ROW_COUNT)
        )
        dependencies = tuple(
            DeferredDependencyFact(
                dependency_row_id=rows[index - 1].row_id,
                owner_row_id=rows[index].row_id,
                field="bom_id",
                keeps_group_together=False,
            )
            for index in range(1, affected_count)
        )
        issue = DeferredIssue(
            issue_id="sha256:" + "b" * 64,
            code="REFERENCE_NOT_FOUND",
            scope=DeferredIssueScope.ROW,
            row_id=rows[0].row_id,
            field="product_id",
            message="The component product is missing in Odoo.",
        )
        process = psutil.Process()
        starting_rss = process.memory_info().rss

        started = perf_counter()
        preview = preview_deferred_scope(
            comparison_id="comparison-25000",
            comparison_hash=HASH,
            execution_snapshot_hash="sha256:" + "c" * 64,
            rows=rows,
            issues=(issue,),
            dependencies=dependencies,
            selected_issue_ids=(issue.issue_id,),
        )
        group_preview_seconds = perf_counter() - started
        ending_rss = process.memory_info().rss

        page_started = perf_counter()
        page_payload = json.dumps(
            [item.portable_dict() for item in preview.omitted_rows[:100]],
            sort_keys=True,
        )
        page_projection_seconds = perf_counter() - page_started

        with tempfile.TemporaryDirectory() as directory:
            manifest_path, workbook_path = write_preflight_outputs(
                golden_result(),
                Path(directory) / "report",
            )
            workbook_started = perf_counter()
            write_review_workbook(
                manifest_path,
                workbook_path,
                deferred_preview=preview,
                deferred_issues=(issue,),
            )
            workbook_seconds = perf_counter() - workbook_started
            workbook_bytes = workbook_path.stat().st_size
        completed_rss = process.memory_info().rss

        metrics = {
            "rows": ROW_COUNT,
            "relationship_edges": len(dependencies),
            "omitted_rows": preview.newly_set_aside_count,
            "remaining_writes": preview.remaining_write_count,
            "group_preview_seconds": group_preview_seconds,
            "page_projection_seconds": page_projection_seconds,
            "workbook_seconds": workbook_seconds,
            "workbook_bytes": workbook_bytes,
            "group_rss_growth_mib": max(0, ending_rss - starting_rss)
            / (1024 * 1024),
            "total_rss_growth_mib": max(0, completed_rss - starting_rss)
            / (1024 * 1024),
            "odoo_requests": 0,
        }
        print("Deferred scope scale qualification: " + json.dumps(metrics))

        self.assertEqual(preview.newly_set_aside_count, affected_count)
        self.assertEqual(preview.remaining_write_count, ROW_COUNT - affected_count)
        self.assertTrue(preview.ready_with_records_set_aside)
        self.assertIn(preview.omitted_rows[0].row_id, page_payload)
        self.assertGreater(workbook_bytes, 0)
        self.assertLess(group_preview_seconds, 5.0)
        self.assertLess(page_projection_seconds, 1.0)
        self.assertLess(workbook_seconds, 30.0)


if __name__ == "__main__":
    unittest.main()
