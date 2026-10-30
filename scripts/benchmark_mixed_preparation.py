"""Measure fictional mixed-join preparation in fresh, bounded worker processes."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import threading
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

import psutil


def _worker(root, workspace, actor, events):
    from impodo.adapters.duckdb import grouped_exact_join_source as source_module
    from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
    from impodo.application.workspace.preparation import (
        preparation_service as service_module,
    )
    from impodo.web.composition.preparation_worker import create_preparation_worker

    service = create_preparation_worker(root, workspace=workspace)
    peak = [0]
    stopped = threading.Event()
    process = psutil.Process()

    def sample():
        while not stopped.is_set():
            peak[0] = max(peak[0], process.memory_info().rss)
            stopped.wait(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    attempts = []
    try:
        for attempt in ("first", "repeat"):
            phases = []
            producer = {}
            prepare = service_module.prepare_bounded_grouped_exact_join_session

            def timed_producer(*args, **kwargs):
                with collect_duckdb_request_timings() as timing:
                    result = prepare(*args, **kwargs)
                producer.update(
                    connections=timing.connection_count,
                    schema_checks=timing.schema_check_count,
                    lock_retries=timing.lock_retry_count,
                )
                return result

            wall, cpu = perf_counter(), process_time()
            peak[0] = process.memory_info().rss
            with (
                patch.object(
                    service_module,
                    "prepare_bounded_grouped_exact_join_session",
                    timed_producer,
                ),
                patch.object(
                    service_module,
                    "stage_browser_mapping",
                    side_effect=AssertionError("materialized source fallback"),
                ),
                patch(
                    "impodo.application.workspace.preparation.quality_service.materialize_staging_run",
                    side_effect=AssertionError("materialized quality fallback"),
                ),
                patch.object(
                    source_module,
                    "_create_narrow_input",
                    wraps=source_module._create_narrow_input,
                ) as scans,
                collect_duckdb_request_timings() as timing,
            ):
                result = service.prepare(
                    workspace.workspace_id,
                    actor=actor,
                    timing=lambda phase, ms, outcome: phases.append(
                        {"phase": phase, "ms": ms, "outcome": outcome}
                    ),
                )
            staging = service.staging.get_current_staging_summary(
                workspace.workspace_id
            )
            quality = service.quality.current_summary(workspace.workspace_id)
            attempts.append(
                dict(
                    attempt=attempt,
                    wall_seconds=perf_counter() - wall,
                    cpu_seconds=process_time() - cpu,
                    peak_worker_mib=peak[0] / 1024**2,
                    producer=producer,
                    other_connections=timing.connection_count,
                    other_schema_checks=timing.schema_check_count,
                    join_projection_scans=scans.call_count,
                    ordinal_scans=3,
                    full_reconstructions=0,
                    phases=phases,
                    eligible_rows=result.eligible_record_count,
                    staging_hash=staging.content_hash,
                    quality_hash=quality.content_hash,
                    storage_bytes=sum(
                        path.stat().st_size
                        for path in Path(root).rglob("*")
                        if path.is_file()
                    ),
                )
            )
        events.put({"attempts": attempts})
    except BaseException as error:
        events.put({"error": repr(error)})
        raise
    finally:
        stopped.set()
        sampler.join()


def main():
    from tests.integration.web.test_mixed_join_preparation import (
        MixedJoinPreparationTests,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = MixedJoinPreparationTests(methodName="runTest")
    fixture.setUp()
    results = []
    try:
        workspace_id, count = fixture._setup()
        fixture._worker(workspace_id, count)
        manager = fixture.context.preparation_jobs
        job = manager.registry.latest_many((workspace_id,))[workspace_id]
        processes = multiprocessing.get_context("spawn")
        for _ in range(args.runs):
            events = processes.Queue()
            child = processes.Process(
                target=_worker,
                args=(manager.root, job.workspace, fixture.actor, events),
            )
            child.start()
            result = events.get(timeout=300)
            child.join(timeout=10)
            if child.is_alive():
                child.terminate()
                child.join()
            if child.exitcode != 0 or "error" in result:
                raise RuntimeError(result)
            results.append(result)
            args.output.write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results, indent=2))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
