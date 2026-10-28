"""Opt-in fresh-process baseline for durable Final review comparison."""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from tests.performance import test_preparation_scale as preparation_scale
from tests.support.identity import resolve_local_actor
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
PREFLIGHT_SCALE_ROWS = int(os.environ.get("IMPODO_PREFLIGHT_SCALE_ROWS", "25000"))
PREFLIGHT_SCALE_TIMEOUT_SECONDS = int(
    os.environ.get("IMPODO_PREFLIGHT_SCALE_TIMEOUT_SECONDS", "900")
)
PREFLIGHT_SCALE_RUNS = int(os.environ.get("IMPODO_PREFLIGHT_SCALE_RUNS", "3"))

_SUMMARY_METRICS = (
    "cpu_seconds",
    "database_connection_count",
    "database_connection_ms",
    "database_lock_retry_count",
    "database_lock_wait_ms",
    "database_mib",
    "database_schema_check_count",
    "database_schema_ms",
    "elapsed_seconds",
    "execution_snapshot_bytes",
    "manifest_bytes",
    "peak_mib",
    "snapshot_bytes",
    "workbook_bytes",
    "workbook_seconds",
)


def summarize_runs(runs: Sequence[dict[str, object]]) -> dict[str, object]:
    """Return medians while leaving the raw fresh-process runs intact."""

    if not runs:
        raise ValueError("At least one preflight baseline run is required")
    phase_names = set.intersection(*(set(dict(run["phases"])) for run in runs))
    route_names = set.intersection(*(set(dict(run["route_baselines"])) for run in runs))
    return {
        "median_metrics": {
            name: statistics.median(float(run[name]) for run in runs)
            for name in _SUMMARY_METRICS
        },
        "median_phase_seconds": {
            name: statistics.median(float(dict(run["phases"])[name]) for run in runs)
            for name in sorted(phase_names)
        },
        "median_route_elapsed_ms": {
            name: statistics.median(
                float(dict(dict(run["route_baselines"])[name])["client_elapsed_ms"])
                for run in runs
            )
            for name in sorted(route_names)
        },
        "run_count": len(runs),
    }


class PreflightScaleHarnessTests(unittest.TestCase):
    """Keep the non-opt-in aggregation logic deterministic and reviewable."""

    def test_summary_reports_medians_without_hiding_raw_runs(self) -> None:
        runs = tuple(
            {
                **{name: float(index) for name in _SUMMARY_METRICS},
                "phases": {"engine": float(index)},
                "route_baselines": {
                    "health_active": {"client_elapsed_ms": float(index)}
                },
            }
            for index in (3, 1, 2)
        )

        summary = summarize_runs(runs)

        self.assertEqual(summary["run_count"], 3)
        self.assertEqual(summary["median_metrics"]["elapsed_seconds"], 2.0)
        self.assertEqual(summary["median_phase_seconds"], {"engine": 2.0})
        self.assertEqual(
            summary["median_route_elapsed_ms"],
            {"health_active": 2.0},
        )

    def test_summary_requires_at_least_one_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "At least one"):
            summarize_runs(())


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREFLIGHT_SCALE") == "1",
    "25,000-row durable preflight scale probe is opt-in",
)
class DurablePreflightScaleTests(unittest.TestCase):
    """Measure a fresh process from frozen retrieval through publication."""

    def test_durable_preflight_workflow(self) -> None:
        if PREFLIGHT_SCALE_ROWS < 1:
            self.fail("The durable preflight scale row count must be positive")
        if PREFLIGHT_SCALE_TIMEOUT_SECONDS < 1:
            self.fail("The durable preflight scale timeout must be positive")
        if PREFLIGHT_SCALE_RUNS < 1:
            self.fail("The durable preflight scale run count must be positive")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            run_root = temporary_root / "run"
            prepared_snapshot = temporary_root / "prepared-snapshot"
            app = create_local_app(
                run_root,
                secret_store=MemorySecretStore(),
                preparation_jobs_enabled=False,
                preflight_jobs_enabled=False,
                odoo_capture_jobs_enabled=False,
                recipe_run_jobs_enabled=False,
                load_jobs_enabled=False,
            )
            context = app.state.context
            artifacts = context.artifacts
            fixture = SimpleNamespace(
                root=run_root,
                artifacts=artifacts,
                context=context,
            )
            fixture_builder = preparation_scale.PreparationWorkflowScaleTests
            workspace_id, _source_hash, _source_bytes = (
                fixture_builder._prepare_project_and_evidence(
                    fixture,
                    row_count=PREFLIGHT_SCALE_ROWS,
                    column_count=3,
                    mapped_field_count=3,
                )
            )
            context.preparation.prepare(
                workspace_id, actor=resolve_local_actor(context)
            )
            review = context.normalization.current_review(workspace_id)
            assert review is not None
            summary, evaluation, _dry_run = review
            for group in evaluation.groups:
                if not group.requires_decision:
                    continue
                summary = context.normalization.decide_group(
                    workspace_id,
                    summary.run_id,
                    group.group_id,
                    approve=True,
                    expected_version=summary.lifecycle_version,
                    actor=resolve_local_actor(context),
                )
            summary = context.normalization.approve(
                workspace_id,
                summary.run_id,
                expected_version=summary.lifecycle_version,
                actor=resolve_local_actor(context),
            )
            self.assertTrue(summary.frozen)

            workspace = context.workspace_states.repository.get(workspace_id)
            workspace_root = context.migration_workspaces.get(
                workspace_id,
                actor=resolve_local_actor(context),
            )
            for source in workspace.source_files:
                artifacts.delete_source(
                    workspace_root.data_version_id,
                    source.stored_name,
                )
            (run_root / "preparation-scale-input.csv").unlink(missing_ok=True)
            shutil.copytree(run_root, prepared_snapshot)

            raw_runs: list[dict[str, object]] = []
            for run_number in range(1, PREFLIGHT_SCALE_RUNS + 1):
                if run_number > 1:
                    shutil.rmtree(run_root)
                    shutil.copytree(prepared_snapshot, run_root)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.performance.preflight_scale_runner",
                        "--root",
                        str(run_root),
                        "--workspace-id",
                        workspace_id,
                        "--rows",
                        str(PREFLIGHT_SCALE_ROWS),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=PREFLIGHT_SCALE_TIMEOUT_SECONDS,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                metrics = json.loads(completed.stdout.strip().splitlines()[-1])
                metrics["run_number"] = run_number
                raw_runs.append(metrics)

            report = {
                "rows": PREFLIGHT_SCALE_ROWS,
                "runs": raw_runs,
                "summary": summarize_runs(raw_runs),
            }
            print(
                "IMPODO_PREFLIGHT_SCALE_BASELINE_JSON="
                + json.dumps(report, sort_keys=True)
            )

            for metrics in raw_runs:
                self.assertEqual(metrics["schema_version"], 1)
                self.assertEqual(metrics["rows"], PREFLIGHT_SCALE_ROWS)
                self.assertEqual(metrics["target_rows"], PREFLIGHT_SCALE_ROWS)
                self.assertEqual(
                    metrics["persisted_decisions"],
                    PREFLIGHT_SCALE_ROWS,
                )
                self.assertEqual(metrics["unchanged"], PREFLIGHT_SCALE_ROWS)
                self.assertEqual(metrics["persisted_snapshots"], 2)
                self.assertEqual(metrics["readiness_runs"], 1)
                self.assertGreater(metrics["metadata_requests"], 0)
                self.assertGreater(metrics["record_requests"], 0)
                self.assertEqual(
                    metrics["record_requests"],
                    metrics["domain_chunks"],
                )
                self.assertGreater(metrics["snapshot_bytes"], 0)
                self.assertGreater(metrics["manifest_bytes"], 0)
                self.assertGreater(metrics["execution_snapshot_bytes"], 0)
                self.assertRegex(metrics["execution_snapshot_hash"], r"^sha256:")
                self.assertGreater(metrics["workbook_bytes"], 0)
                self.assertGreater(metrics["database_connection_count"], 0)
                self.assertLessEqual(metrics["database_connection_count"], 70)
                self.assertGreater(metrics["database_schema_check_count"], 0)
                self.assertLessEqual(metrics["database_schema_check_count"], 16)
                self.assertEqual(metrics["database_lock_retry_count"], 0)
                self.assertTrue(metrics["status_completed"])
                phases = metrics["phases"]
                for phase in (
                    "load_frozen_input",
                    "plan_requirements",
                    "read_snapshot",
                    "engine",
                    "build_report",
                    "write_manifest",
                    "write_execution_snapshot",
                    "publish_evidence",
                ):
                    self.assertIn(phase, phases)
                routes = metrics["route_baselines"]
                self.assertEqual(routes["compare_start"]["status_code"], 303)
                self.assertEqual(routes["progress_active"]["status_code"], 200)
                self.assertEqual(routes["status_active"]["status_code"], 200)
                self.assertEqual(routes["health_active"]["status_code"], 200)
                self.assertEqual(
                    routes["summary_action_rich"]["status_code"],
                    200,
                )
                self.assertEqual(routes["load_review"]["status_code"], 200)
                for route in routes.values():
                    self.assertEqual(route["database_lock_retry_count"], 0)
                self.assertEqual(
                    routes["health_active"]["database_connection_count"],
                    0,
                )
                self.assertLessEqual(
                    routes["status_active"]["database_connection_count"],
                    2,
                )
                self.assertEqual(
                    routes["status_active"]["database_schema_check_count"],
                    0,
                )
                self.assertLessEqual(
                    routes["progress_active"]["database_connection_count"],
                    2,
                )
                self.assertEqual(
                    routes["progress_active"]["database_schema_check_count"],
                    0,
                )
                self.assertLessEqual(
                    routes["compare_start"]["database_connection_count"],
                    16,
                )
                self.assertLessEqual(
                    routes["compare_start"]["database_schema_check_count"],
                    2,
                )
                self.assertLessEqual(
                    routes["summary_action_rich"]["database_connection_count"],
                    8,
                )
                self.assertLessEqual(
                    routes["summary_action_rich"]["database_schema_check_count"],
                    32,
                )
                self.assertLessEqual(
                    routes["load_review"]["database_connection_count"],
                    75,
                )
                self.assertLessEqual(
                    routes["load_review"]["database_schema_check_count"],
                    16,
                )

            self.assertEqual(
                len({metrics["result_hash"] for metrics in raw_runs}),
                1,
            )


if __name__ == "__main__":
    unittest.main()
