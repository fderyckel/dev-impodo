"""Opt-in historical-scale diagnostic for durable preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from impodo.artifacts import LocalArtifactStore
from impodo.web.app import create_local_app

from tests import test_preparation_scale as preparation_scale


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_SCALE_ROWS = int(os.environ.get("IMPODO_PREFLIGHT_SCALE_ROWS", "25000"))


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREFLIGHT_SCALE") == "1",
    "25,000-row durable preflight scale probe is opt-in",
)
class DurablePreflightScaleTests(unittest.TestCase):
    """Measure a fresh process from frozen retrieval through publication."""

    def test_durable_preflight_workflow(self) -> None:
        if PREFLIGHT_SCALE_ROWS < 1:
            self.fail("The durable preflight scale row count must be positive")
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            artifacts = LocalArtifactStore(root)
            app = create_local_app(root, artifact_store=artifacts)
            context = app.state.context
            fixture = SimpleNamespace(
                root=root,
                artifacts=artifacts,
                context=context,
            )
            fixture_builder = preparation_scale.PreparationWorkflowScaleTests
            project_id, _source_hash, _source_bytes = (
                fixture_builder._prepare_project_and_evidence(
                    fixture,
                    row_count=PREFLIGHT_SCALE_ROWS,
                    column_count=3,
                    mapped_field_count=3,
                )
            )
            context.preparation.prepare(project_id, actor=context.actor)
            review = context.normalization.current_review(project_id)
            assert review is not None
            summary, evaluation, _dry_run = review
            for group in evaluation.groups:
                if not group.requires_decision:
                    continue
                summary = context.normalization.decide_group(
                    project_id,
                    summary.run_id,
                    group.group_id,
                    approve=True,
                    expected_version=summary.lifecycle_version,
                    actor=context.actor,
                )
            summary = context.normalization.approve(
                project_id,
                summary.run_id,
                expected_version=summary.lifecycle_version,
                actor=context.actor,
            )
            self.assertTrue(summary.frozen)

            project = context.projects.repository.get(project_id)
            for source in project.source_files:
                artifacts.delete_source(project_id, source.stored_name)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.preflight_scale_runner",
                    "--root",
                    str(root),
                    "--project-id",
                    project_id,
                    "--rows",
                    str(PREFLIGHT_SCALE_ROWS),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
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
