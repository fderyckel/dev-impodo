"""Exercise real finding-heavy workers, phase-boundary cancellation and retry."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from time import perf_counter, sleep
from unittest.mock import patch

from impodo.application.workspace.preparation.job_models import (
    PreparationJobStatus,
    PreparationPhase,
    PreparationWorkspace,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.app import create_local_app
from impodo.web.composition import preparation_job_manager as workers
from tests.integration.artifacts.test_quality_finding_preparation import _FindingFixture
from tests.support.finding_worker_probe import (
    finding_worker,
    finding_worker_waiting_for_cancel,
)


class _WorkerFixture(_FindingFixture):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        parent = Path(self.temporary.name)
        self.root = parent / "sources"
        self.root.mkdir()
        self.store_root = parent / "store"
        self.app = create_local_app(self.store_root)
        self.context = self.app.state.context
        self.artifacts = self.context.artifacts
        self.manager = self.context.preparation_jobs
        self.evidence_root = parent / "worker-evidence"

    def tearDown(self):
        try:
            self.manager.shutdown()
        finally:
            self.temporary.cleanup()

    def _workspace(self):
        context, actor = self.context, self.actor
        workspace = context.migration_workspaces.get(self.workspace_id, actor=actor)
        return PreparationWorkspace.from_context(
            workspace,
            context.data_versions.get(workspace.data_version_id, actor=actor),
            context.migration_runs.get(workspace.migration_run_id, actor=actor),
        )

    def _attempt(self, rows, *, stop=False, previous=None, timeout=90):
        reports = set(self.evidence_root.glob("worker-*.json"))
        target = finding_worker_waiting_for_cancel if stop else finding_worker
        with patch.object(workers, "_run_preparation_worker", target):
            if previous is None:
                job = self.manager.enqueue(
                    self.workspace_id,
                    "Finding-heavy preparation",
                    rows,
                    actor=self.actor,
                    workspace=self._workspace(),
                )
            else:
                job = self.manager.retry(
                    self.workspace_id,
                    previous.job_id,
                    "Finding-heavy preparation",
                    rows,
                    actor=self.actor,
                    workspace=self._workspace(),
                )
        deadline = perf_counter() + timeout
        while perf_counter() < deadline:
            current = self.manager.get(self.workspace_id, job.job_id)
            if (
                stop
                and current.phase is PreparationPhase.NORMALIZING
                and not current.cancel_requested
            ):
                self.manager.cancel(self.workspace_id, job.job_id)
            if (
                current.terminal
                and not self.manager.worker_alive(job.job_id)
                and self.manager.performance_evidence(job.job_id).get("event_kind")
                == "attempt_finished"
            ):
                break
            sleep(0.05)
        else:
            self.fail("The real preparation worker did not exit")
        expected = (
            PreparationJobStatus.CANCELLED if stop else PreparationJobStatus.SUCCEEDED
        )
        self.assertEqual(current.status, expected, current.failure_message)
        self.assertEqual(current.cancel_requested, stop)
        self.assertFalse(self.manager.worker_alive(job.job_id))
        new_reports = set(self.evidence_root.glob("worker-*.json")) - reports
        self.assertEqual(len(new_reports), 1)
        probe = json.loads(new_reports.pop().read_text(encoding="utf-8"))
        self.assertEqual(probe["active_finding_stores_before_normalization"], 1)
        self.assertEqual(probe["remaining_finding_stores"], 0)
        performance = self.manager.performance_evidence(job.job_id)
        self.assertIs(performance["measurement_complete"], True)
        self.assertEqual(
            performance["environment"]["build_id"],
            self.manager.build_contract.application_build_id,
        )
        self.assertEqual(
            performance["configuration"]["polars_threads_requested"],
            performance["configuration"]["polars_threads_effective"],
        )
        return current, performance, probe

    def _reopened_quality(self, quality):
        reopened = create_local_app(self.store_root).state.context.preparation
        self.assertEqual(
            reopened.quality.quality.get_quality_run(
                self.workspace_id, quality.run_id
            ).to_json(),
            self.context.preparation.quality.quality.get_quality_run(
                self.workspace_id, quality.run_id
            ).to_json(),
        )
        page = reopened.quality.quality.get_quality_review_page(
            self.workspace_id,
            quality.run_id,
            status="quarantined",
            page=2,
            page_size=17,
        )
        self.assertEqual(page.matching_count, quality.quarantined_count)
        self.assertEqual(len(page.items), 17)


class FindingPreparationWorkerTests(_WorkerFixture):
    def _recovery(self, major, mode):
        self.configure(257, 193, major=major, mode=mode)
        first, _, _ = self._attempt(257)
        quality = self.assert_counts(257, 193)
        normalization = self.context.preparation.normalization.current_summary(
            self.workspace_id
        )
        stopped, _, _ = self._attempt(257, stop=True)
        self.assertEqual(
            self.context.preparation.normalization.current_summary(self.workspace_id),
            normalization,
        )
        self.assertEqual(
            self.context.preparation.quality.current_summary(
                self.workspace_id
            ).content_hash,
            quality.content_hash,
        )
        retried, _, _ = self._attempt(257, previous=stopped)
        self.assertNotEqual(retried.job_id, stopped.job_id)
        self.assertNotEqual(first.job_id, retried.job_id)
        retried_quality = self.assert_counts(257, 193)
        self.assertEqual(retried_quality.content_hash, quality.content_hash)
        self.assertEqual(
            self.context.preparation.normalization.current_summary(
                self.workspace_id
            ).content_hash,
            normalization.content_hash,
        )
        repository = self.context.preparation.staging
        with repository._connect(
            repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        ) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, COUNT(*) FROM preparation_session GROUP BY status ORDER BY status"
                ).fetchall(),
                [("FAILED", 1), ("PUBLISHED", 2)],
            )
        self._reopened_quality(retried_quality)

    def test_odoo19_local_cancellation_and_retry(self):
        self._recovery(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote_cancellation_and_retry(self):
        self._recovery(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local_cancellation_and_retry(self):
        self._recovery(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote_cancellation_and_retry(self):
        self._recovery(20, OdooConnectionMode.REMOTE)


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_FINDING_WORKER_SCALE") == "1",
    "finding-heavy real worker scale is opt-in",
)
class FindingPreparationWorkerScaleTests(_WorkerFixture):
    def test_54191_rows_publish_all_findings_in_first_and_repeat_workers(self):
        self.configure(54_191, 32_977)
        hashes = []
        for name in ("first", "repeat"):
            _, performance, probe = self._attempt(54_191, timeout=600)
            summary = self.assert_counts(54_191, 32_977)
            hashes.append(
                (
                    summary.content_hash,
                    self.context.preparation.normalization.current_summary(
                        self.workspace_id
                    ).content_hash,
                )
            )
            repository = self.context.preparation.quality.quality
            with repository._connect(
                repository.workspace_directory(self.workspace_id)
                / "workspace-engine.duckdb"
            ) as connection:
                finding_count = connection.execute(
                    "SELECT COUNT(*) FROM quality_issue WHERE run_id = ?",
                    [summary.run_id],
                ).fetchone()[0]
            self.assertEqual(finding_count, 98_931)
            # The service's validation, transform, staging, quality and
            # normalization scopes permit 5/6/10/12/8 opens and 3/5/7/9/6
            # checks. The fixed worker composition adds no database opens.
            self.assertLessEqual(performance["database"]["connection_count"], 41)
            self.assertLessEqual(performance["database"]["schema_check_count"], 30)
            self.assertLessEqual(probe["sql_statements"], 550 + 8 * 55 + 8 * 99)
            self.assertGreater(performance["resources"]["cpu_seconds"], 0)
            self.assertGreater(
                performance["resources"]["sampled_peak_worker_rss_bytes"], 0
            )
            print(
                json.dumps(
                    {
                        "attempt": name,
                        "rows": 54_191,
                        "findings": finding_count,
                        "performance": performance,
                        "probe": probe,
                    }
                ),
                flush=True,
            )
        self.assertEqual(hashes[0], hashes[1])
        self._reopened_quality(summary)
