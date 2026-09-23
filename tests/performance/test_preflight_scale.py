"""Opt-in historical-scale diagnostic for durable preflight."""

from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from impodo.web.app import create_local_app

from tests.performance import test_preparation_scale as preparation_scale


ROOT = REPOSITORY_ROOT
PREFLIGHT_SCALE_ROWS = int(os.environ.get("IMPODO_PREFLIGHT_SCALE_ROWS", "25000"))
PREFLIGHT_SCALE_TIMEOUT_SECONDS = int(
    os.environ.get("IMPODO_PREFLIGHT_SCALE_TIMEOUT_SECONDS", "900")
)


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
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            app = create_local_app(root)
            context = app.state.context
            artifacts = context.artifacts
            fixture = SimpleNamespace(
                root=root,
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
            context.preparation.prepare(workspace_id, actor=context.actor)
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
                    actor=context.actor,
                )
            summary = context.normalization.approve(
                workspace_id,
                summary.run_id,
                expected_version=summary.lifecycle_version,
                actor=context.actor,
            )
            self.assertTrue(summary.frozen)

            workspace = context.workspace_states.repository.get(workspace_id)
            workspace_root = context.migration_workspaces.get(
                workspace_id,
                actor=context.actor,
            )
            for source in workspace.source_files:
                artifacts.delete_source(
                    workspace_root.data_version_id,
                    source.stored_name,
                )
            (root / "preparation-scale-input.csv").unlink(missing_ok=True)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.performance.preflight_scale_runner",
                    "--root",
                    str(root),
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
            print(
                "Durable preflight scale probe: "
                + ", ".join(
                    f"{name}={value}"
                    for name, value in sorted(metrics.items())
                )
            )

            self.assertEqual(metrics["rows"], PREFLIGHT_SCALE_ROWS)
            self.assertEqual(metrics["target_rows"], PREFLIGHT_SCALE_ROWS)
            self.assertEqual(metrics["persisted_decisions"], PREFLIGHT_SCALE_ROWS)
            self.assertEqual(metrics["unchanged"], PREFLIGHT_SCALE_ROWS)
            self.assertEqual(metrics["persisted_snapshots"], 2)
            self.assertEqual(metrics["readiness_runs"], 1)
            self.assertGreater(metrics["metadata_requests"], 0)
            self.assertGreater(metrics["record_requests"], 0)
            self.assertEqual(metrics["record_requests"], metrics["domain_chunks"])
            self.assertGreater(metrics["snapshot_bytes"], 0)
            self.assertGreater(metrics["manifest_bytes"], 0)
            self.assertGreater(metrics["execution_snapshot_bytes"], 0)
            self.assertGreater(metrics["workbook_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
