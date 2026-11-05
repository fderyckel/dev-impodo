"""Measure Stage 2, 3 and 5 against isolated fictional local and remote targets.

Run with the checkout toolchain from the repository root. This script never
opens a running application's stores or contacts Odoo. Compare output from the
same script and fixtures on a preserved before/after source snapshot.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.business_keys import BUSINESS_KEY_POLICY_VERSION
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


def measure(callback, *, samples: int, expected_status: int) -> dict[str, object]:
    """Warm rendering once, then retain raw value-free request measurements."""

    warm = callback()
    if warm.status_code != expected_status:
        raise AssertionError(f"Unexpected fixture response {warm.status_code}: {warm.text[:1000]}")
    measurements = []
    for _ in range(samples):
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            started = perf_counter()
            response = callback()
            elapsed_ms = (perf_counter() - started) * 1000
        if response.status_code != expected_status:
            raise AssertionError(f"Unexpected fixture response {response.status_code}: {response.text[:1000]}")
        measurements.append({
            "elapsed_ms": elapsed_ms,
            "sql_statements": len(statements),
            "column_catalogue_reads": sum("duckdb_columns()" in sql for sql in statements),
            **asdict(timings),
        })
    return {
        "median": {
            key: statistics.median(row[key] for row in measurements)
            for key in measurements[0]
        },
        "samples": measurements,
    }


def small_routes(major: int, mode: str, samples: int) -> dict[str, object]:
    """Exercise saved pages, formula parsing, key confirmation and recovery."""

    fixture = ProjectSetupBrowserTestCase()
    fixture.setUp()
    try:
        workspace_id, dataset, _ = fixture._mapping_ready_workspace(
            scalar_field_count=2, odoo_major=major, connection_mode=OdooConnectionMode(mode),
        )
        prefix = f"/workspaces/{workspace_id}/"
        schema = fixture.app.state.context.queries.get_odoo_schema_catalog(workspace_id)
        routes = {
            area: measure(lambda area=area: fixture.client.get(prefix + area),
                          samples=samples, expected_status=200)
            for area in ("schema", "mapping", "summary")
        }
        for area, data, status in (
            ("mapping/formula-validation", {
                "dataset_id": dataset.dataset_id, "formula": "'Ready'",
            }, 200),
            ("schema/govern", {
                "expected_schema_hash": schema.content_hash,
                "expected_business_key_policy_version": str(BUSINESS_KEY_POLICY_VERSION),
                "primary_key_field_0": "ref", "key_description_0": "Unique reference",
            }, 303),
            ("mapping/order", {"action": "unsupported"}, 422),
        ):
            routes[area] = measure(lambda area=area, data=data: fixture._post(
                prefix + area, {"csrf_token": fixture.csrf, **data},
            ), samples=samples, expected_status=status)
        return routes
    finally:
        fixture.tearDown()


def review_routes(major: int, mode: str, samples: int) -> dict[str, object]:
    """Measure real prepared comparison groups and local preparation intake."""

    fixture = HierarchyWorkflowFixture()
    fixture.setUp()
    try:
        workspace_id, _ = fixture.hierarchy(major=major, mode=mode)
        fixture.prepare(workspace_id)
        fixture.approve(workspace_id)
        fixture.expect_business_writes(workspace_id)
        target = HierarchyOdoo(fixture.context.queries.get_odoo_schema_catalog(workspace_id))
        target.records[("product.category", 900)] = {"name": "Machines", "parent_id": False}
        run_local_operation_sync(
            fixture.context.preflight.compare, workspace_id,
            reader=target.compare, actor=fixture.actor,
        )
        request_count = len(target.requests)
        prefix = f"/workspaces/{workspace_id}/summary"
        routes = {
            name: measure(lambda suffix=suffix: fixture.client.get(prefix + suffix),
                          samples=samples, expected_status=200)
            for name, suffix in (("prepared_summary", ""), ("deferred_groups", "/deferred-groups"))
        }
        routes["deferred_preview"] = measure(lambda: fixture._post(
            prefix + "/deferred-groups/preview", {"csrf_token": fixture.csrf, "select_all": "1"},
        ), samples=samples, expected_status=200)
        # Isolate local intake; process startup belongs to a separate benchmark.
        with patch.object(fixture.context.preparation_jobs, "enqueue",
                          return_value=SimpleNamespace(job_id="benchmark", active=True)):
            routes["preparation_intake"] = measure(lambda: fixture._post(
                prefix + "/check", {"csrf_token": fixture.csrf},
            ), samples=samples, expected_status=303)
        assert len(target.requests) == request_count
        assert not target.writes
        return routes
    finally:
        fixture.tearDown()


def main() -> None:
    """Print medians and raw samples for each supported major and access mode."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--odoo-major", type=int, choices=(19, 20), action="append")
    parser.add_argument("--access-mode", choices=("LOCAL", "REMOTE"), action="append")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    cases = []
    for major in args.odoo_major or (19, 20):
        for mode in args.access_mode or ("LOCAL", "REMOTE"):
            cases.append({
                "odoo_major": major, "access_mode": mode,
                "routes": {**small_routes(major, mode, args.samples),
                           **review_routes(major, mode, args.samples)},
            })
    encoded = json.dumps({"evidence": "isolated fictional fixtures", "cases": cases}, indent=2)
    if args.output is not None:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
