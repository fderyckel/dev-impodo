"""Measure Stage 3 advice and full saved-draft checks in isolated fictional workspaces."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import threading
from contextlib import contextmanager
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path
from time import perf_counter, process_time
from types import SimpleNamespace
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.application.workspace.mapping.order_service import MatchingOrderService
from impodo.domain.mapping.contracts import MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES
from impodo.domain.staging.scale import (
    LOOKUP_DERIVED_BROWSER_EVALUATION_ROW_LIMIT,
    LOOKUP_DERIVED_OUTPUT_ROW_LIMIT,
)
from impodo.domain.workspace.derived_entities import RelatedDatasetLink
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture
from tests.support.matching_order_workspace import ordering_workspace


@contextmanager
def measurement(result):
    """Sample current-process RSS through publication and retain raw timings."""
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = baseline
    stop = threading.Event()

    def sample():
        nonlocal peak
        while not stop.wait(0.01):
            peak = max(peak, process.memory_info().rss)

    sampler = threading.Thread(target=sample)
    sampler.start()
    wall, cpu = perf_counter(), process_time()
    try:
        yield
    finally:
        result["elapsed_ms"] = round((perf_counter() - wall) * 1000, 3)
        result["cpu_ms"] = round((process_time() - cpu) * 1000, 3)
        stop.set()
        sampler.join()
        result["baseline_rss_bytes"] = baseline
        result["sampled_peak_rss_bytes"] = max(peak, process.memory_info().rss)


def structural(size):
    """Exercise the same recommendation service the page calls on a reversed chain."""
    ids = tuple(f"table_{index:04}" for index in range(size))
    selection = SimpleNamespace(
        datasets=tuple(
            SimpleNamespace(dataset_id=value, source=None) for value in reversed(ids)
        )
    )
    schema = SimpleNamespace(models=())
    links = tuple(RelatedDatasetLink(a, b, (), ()) for a, b in pairwise(ids))
    service = MatchingOrderService()
    result = {"datasets": size, "edges": len(links)}
    with measurement(result):
        recommendation = service.recommend(selection, schema, None, related_links=links)
    assert recommendation.ordered_dataset_ids == ids
    return result


def live(shape, fixture):
    """Time the normal start route, actual frozen source reads and durable publication."""
    limit = MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES
    rows, categories = {
        "direct": (limit, 1),
        "related": (limit + 1, limit),
        "derived": (
            LOOKUP_DERIVED_BROWSER_EVALUATION_ROW_LIMIT,
            LOOKUP_DERIVED_OUTPUT_ROW_LIMIT,
        ),
    }[shape]
    wid, reads = ordering_workspace(
        fixture, shape=shape, rows=rows, categories=categories
    )
    service = fixture.context.matching_order
    worker = service._run_live_check
    completed = threading.Event()
    worker_metrics = {}

    def measured_worker(*args):
        try:
            with collect_duckdb_request_timings() as db:
                worker(*args)
                worker_metrics.update(
                    opens=db.connection_count,
                    schema_checks=db.schema_check_count,
                    lock_retries=db.lock_retry_count,
                )
        finally:
            completed.set()

    result = {
        "shape": shape,
        "owner_rows": rows,
        "category_keys": 0 if shape == "direct" else categories,
    }
    with (
        patch.object(service, "_run_live_check", measured_worker),
        count_duckdb_statements() as sql,
        collect_duckdb_request_timings() as admission,
        measurement(result),
    ):
        started = fixture.client.post(
            f"/workspaces/{wid}/mapping/order/check",
            json={"entries": [["csrf_token", fixture.csrf]]},
            headers={**POST_HEADERS, "Accept": "application/json"},
        )
        assert started.status_code == 202, started.status_code
        assert completed.wait(240), "The saved check did not finish"
    check = service.current_check(wid, actor=fixture.actor)
    assert (
        check is not None
        and check.check_id == started.json()["check_id"]
        and not check.schema_changed
    )
    assert len(reads) == 1
    metadata, records = reads[0]
    assert check.identity_results and any(
        i.source_row_count == rows for i in check.identity_results
    )
    if shape != "direct":
        (health,) = check.relationship_health_results
        assert health.checked and health.incoming_count == rows, health
    result.update(
        sql_statements=len(sql),
        catalogue_reads=sum("duckdb_columns()" in s for s in sql),
        admission_opens=admission.connection_count,
        admission_schema_checks=admission.schema_check_count,
        admission_lock_retries=admission.lock_retry_count,
        worker=worker_metrics,
        reader_calls=len(reads),
        metadata_requests=len(metadata),
        record_requests=len(records),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample", choices=("1", "10", "100", "1000", "direct", "related", "derived")
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sample and args.sample.isdigit():
        print(json.dumps(structural(int(args.sample))))
    elif args.sample:
        fixture = HierarchyWorkflowFixture("runTest")
        fixture.setUp()
        try:
            print(json.dumps(live(args.sample, fixture)))
        finally:
            fixture.tearDown()
            fixture.doCleanups()
    else:
        if args.runs < 1:
            parser.error("--runs must be positive")
        result = {
            "build": PROCESS_BUILD_CONTRACT.application_build_id,
            "platform": platform.system(),
            "platform_release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "libraries": {lib: version(lib) for lib in ("duckdb", "polars", "psutil")},
            "target": "Fictional Odoo 20 REMOTE adapter with empty target record sets",
            "samples": [],
        }
        for name in ("1", "10", "100", "1000", "direct", "related", "derived"):
            for repeat in range(args.runs):
                output = subprocess.check_output(
                    [sys.executable, __file__, "--sample", name], text=True
                )
                result["samples"].append(
                    {"case": name, "fresh_process": repeat + 1, **json.loads(output)}
                )
        encoded = json.dumps(result, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")


if __name__ == "__main__":
    main()
