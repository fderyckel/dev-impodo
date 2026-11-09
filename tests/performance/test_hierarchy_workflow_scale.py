"""Opt-in measurements of accepted files on the generated-hierarchy route."""

import json
import os
import unittest
from itertools import product
from pathlib import Path
from time import perf_counter

import psutil

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.business_intent import (
    BusinessIntent,
    BusinessModelIntent,
    ExpectedCount,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.benchmark_dependency_execution import _PeakSampler
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import (
    EXISTING_CATEGORY_PATHS,
    PRODUCT_ROWS,
    HierarchyWorkflowFixture,
)


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


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_DEPENDENCY_INTENT_SCALE") == "1",
    "The 25,000-row dependency and intent qualification is opt-in",
)
class DependencyIntentScaleTests(HierarchyWorkflowFixture):
    def test_approved_hierarchy_and_intent_reassessment_at_25000_rows(self):
        # Reuse one complete target path and generate a wide four-level tree.
        # Repeated leaf labels are distinct under their full parent paths.
        row_count = 25_000
        reused_path = EXISTING_CATEGORY_PATHS[3]
        new_leaves = tuple(
            (
                f"Division {division}",
                f"Group {group}",
                f"Family {family}",
                f"Leaf {leaf}",
            )
            for division, group, family, leaf in product(
                range(2), range(5), range(5), range(5)
            )
        )
        new_paths = {leaf[:depth] for leaf in new_leaves for depth in range(1, 5)}
        rows = (
            ("SCALE000000", *reused_path),
            *(
                (f"SCALE{index:06d}", *new_leaves[index % len(new_leaves)])
                for index in range(1, row_count)
            ),
        )
        workspace, _revision = self.hierarchy(major=20, mode="REMOTE", rows=rows)
        schema = self.context.queries.get_odoo_schema_catalog(workspace)
        target = HierarchyOdoo(schema)
        metadata_requests = 0

        def read(requirements):
            nonlocal metadata_requests
            metadata_requests += len(requirements.metadata_requests)
            return target.compare(requirements)

        started = perf_counter()
        with (
            _PeakSampler(psutil.Process()) as memory,
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as statements,
        ):
            staging, quality = self.prepare(workspace)
            prepared_count = row_count + len(new_paths) + len(reused_path)
            self.assertEqual(len(staging.rows), prepared_count)
            del staging
            self.assertEqual(quality.ready_count, prepared_count)
            self.assertEqual((quality.quarantined_count, quality.blocked_count), (0, 0))
            run_local_operation_sync(self.approve, workspace)
            category_rule = BusinessModelIntent(
                "product.category",
                "create",
                creates=ExpectedCount(len(new_paths), len(new_paths)),
            )
            rules = BusinessIntent(
                (
                    category_rule,
                    BusinessModelIntent(
                        "product.template", "create", creates=ExpectedCount(maximum=0)
                    ),
                )
            )
            self.expect_business_writes(workspace, intent=rules)
            opens_before = timings.connection_count
            checks_before = timings.schema_check_count
            report = self.context.preflight.compare(
                workspace,
                reader=read,
                actor=self.actor,
                local_operation=run_local_operation_sync,
            )
            comparison_connections = timings.connection_count - opens_before
            comparison_schema_checks = timings.schema_check_count - checks_before
            self.assertEqual(report.create_count, row_count + len(new_paths))
            self.assertEqual(report.unchanged_count, len(reused_path))
            snapshot = run_local_operation_sync(
                self.context.preflight.current_execution_snapshot, workspace
            )
            self.assertIsNotNone(snapshot)
            snapshot_hash = snapshot.semantic_hash
            # Every Product refers to its incoming Category row, including
            # the Category that comparison matched to an existing record.
            expected_edges = row_count + sum(len(path) > 1 for path in new_paths)
            self.assertEqual(snapshot.relationship_plan.edge_count, expected_edges)
            self.assertEqual(snapshot.relationship_plan.blocker_count, 0)
            scheduled = {
                row_id
                for component in snapshot.relationship_plan.components
                for row_id in component.row_ids
            }
            self.assertEqual(len(scheduled), row_count + len(new_paths))
            self.assertTrue(
                scheduled.isdisjoint(
                    row.row_id
                    for row in snapshot.rows
                    if row.disposition == "UNCHANGED"
                )
            )
            blocked = run_local_operation_sync(
                self.context.preflight.business_intent_review,
                workspace,
                snapshot_hash,
                actor=self.actor,
            )
            self.assertEqual(blocked.state, "DEVIATION")
            products = next(
                item for item in blocked.models if item.model == "product.template"
            )
            self.assertEqual((products.creates, products.create_max), (row_count, 0))

            comparison_request_count = len(target.requests)
            accepted = BusinessIntent(
                (
                    category_rule,
                    BusinessModelIntent(
                        "product.template",
                        "create",
                        creates=ExpectedCount(row_count, row_count),
                    ),
                )
            )
            opens_before = timings.connection_count
            checks_before = timings.schema_check_count
            self.expect_business_writes(workspace, intent=accepted)
            assessment = run_local_operation_sync(
                self.context.preflight.reassess_business_intent,
                workspace,
                actor=self.actor,
            )
            reassessment_connections = timings.connection_count - opens_before
            reassessment_schema_checks = timings.schema_check_count - checks_before
            self.assertTrue(assessment.ready)
            ready = run_local_operation_sync(
                self.context.preflight.business_intent_review,
                workspace,
                snapshot_hash,
                actor=self.actor,
            )
            self.assertTrue(ready.ready)
            self.assertNotEqual(blocked.assessment_hash, ready.assessment_hash)
            self.assertEqual(ready.snapshot_hash, snapshot_hash)
            self.assertEqual(len(target.requests), comparison_request_count)
            self.assertEqual(target.writes, [])
        self.assertGreater(comparison_request_count, 0)
        self.assertLess(comparison_request_count, row_count)
        self.assertEqual(timings.lock_retry_count, 0)
        print(
            "IMPODO_DEPENDENCY_INTENT_SCALE_JSON="
            + json.dumps(
                {
                    "odoo_major": 20,
                    "connection_mode": "REMOTE",
                    "physical_rows": row_count,
                    "prepared_records": prepared_count,
                    "dependency_edges": snapshot.relationship_plan.edge_count,
                    "elapsed_seconds": round(perf_counter() - started, 3),
                    "peak_process_rss_bytes": memory.peak_bytes,
                    "connections": timings.connection_count,
                    "schema_checks": timings.schema_check_count,
                    "sql_statements": len(statements),
                    "lock_retries": timings.lock_retry_count,
                    "fixture_metadata_requests": metadata_requests,
                    "fixture_record_requests": comparison_request_count,
                    "comparison_connections": comparison_connections,
                    "comparison_schema_checks": comparison_schema_checks,
                    "reassessment_connections": reassessment_connections,
                    "reassessment_schema_checks": reassessment_schema_checks,
                },
                sort_keys=True,
            )
        )
