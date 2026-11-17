"""Publish complete review evidence when many rows have real findings."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch

import psutil

from impodo.adapters.duckdb import preparation_quality_spill
from impodo.adapters.duckdb import preparation_quality_index
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.preparation.quality import evaluate_quality
from impodo.domain.errors import ReadinessError
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.performance import test_preparation_scale as fixtures
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class _FindingFixture(unittest.TestCase):
    setUp = fixtures.BoundedPreparationParityTests.setUp
    tearDown = fixtures.BoundedPreparationParityTests.tearDown

    def configure(self, rows, invalid, *, major=19, mode=OdooConnectionMode.LOCAL):
        original_values = fixtures._source_values
        original_catalog = fixtures.OdooSchemaCatalog
        original_target = self.context.workspace_states.update_target

        def source(index, *args, **kwargs):
            values = original_values(index, *args, **kwargs)
            return ("", *values[1:]) if index < invalid else values

        def catalog(*args, **kwargs):
            return replace(original_catalog(*args, **kwargs), odoo_version=f"{major}.0")

        def target(*args, **kwargs):
            kwargs["odoo_connection_mode"] = mode.value
            if mode is OdooConnectionMode.REMOTE:
                kwargs["odoo_base_url"] = "https://odoo.example.test"
            return original_target(*args, **kwargs)

        with (
            patch.object(fixtures, "_source_values", source),
            patch.object(fixtures, "OdooSchemaCatalog", catalog),
            patch.object(self.context.workspace_states, "update_target", target),
            patch.object(fixtures, "PREPARATION_SCALE_WORKLOAD", "products"),
            patch.object(fixtures, "PREPARATION_SCALE_EFFECT_FIELDS", 1),
        ):
            self.workspace_id, _, _ = (
                fixtures.PreparationWorkflowScaleTests._prepare_project_and_evidence(
                    self,
                    row_count=rows,
                    column_count=8,
                    mapped_field_count=6,
                )
            )
        self.actor = resolve_local_actor(self.context)

    def prepare(self, *, progress=None):
        with (
            patch.object(
                fixtures.preparation_module,
                "stage_browser_mapping",
                side_effect=AssertionError("whole-run source fallback"),
            ),
            patch.object(
                fixtures.quality_module,
                "evaluate_quality",
                side_effect=AssertionError("whole-run quality fallback"),
            ),
        ):
            return self.context.preparation.prepare(
                self.workspace_id, actor=self.actor, progress=progress
            )

    def assert_counts(self, rows, invalid):
        service = self.context.preparation
        quality = service.quality.current_summary(self.workspace_id)
        normalization = service.normalization.current_summary(self.workspace_id)
        self.assertEqual(quality.ready_count, rows - invalid)
        self.assertEqual(quality.quarantined_count, invalid)
        self.assertEqual(quality.blocked_count, 0)
        self.assertEqual(normalization.eligible_record_count, rows - invalid)
        self.assertEqual(normalization.set_aside_record_count, invalid)
        # This fresh read occurs after the temporary evaluation store is closed.
        page = service.quality.quality.get_quality_review_page(
            self.workspace_id,
            quality.run_id,
            status="quarantined",
            page_size=17,
        )
        self.assertEqual(page.matching_count, invalid)
        self.assertTrue(page.items)
        return quality


class QualityFindingPreparationTests(_FindingFixture):
    def test_total_findings_can_exceed_the_in_memory_allowance(self):
        self.configure(257, 193)
        with patch.object(
            preparation_quality_index, "QUALITY_FINDING_INDEX_MAX_BYTES", 32 * 1024
        ):
            with (
                patch(
                    "impodo.application.workspace.preparation.preparation_service.QualityFindingStoreFactory",
                    type("InMemoryOnly", (), {}),
                ),
                self.assertRaisesRegex(
                    (ReadinessError, WorkspaceError), "bounded review memory budget"
                ),
            ):
                self.prepare()
            self.prepare()
        self.assert_counts(257, 193)

    def _assert_parity(self, major, mode):
        self.configure(257, 193, major=major, mode=mode)
        self.prepare()
        summary = self.assert_counts(257, 193)
        service = self.context.preparation
        staging = service.staging.get_canonical_staging_run(
            self.workspace_id,
            summary.staging_run_id,
        )
        expected = evaluate_quality(
            workspace_state=service.workspaces.get(self.workspace_id),
            staging=staging,
            physical_rows={
                dataset_id: tuple(range(2, 259))
                for row in staging.rows[:1]
                for dataset_id in row.lineage.physical_sources
            },
            ruleset=service.quality.current_ruleset(self.workspace_id),
            published_staging_content_hash=summary.staging_content_hash,
        )
        actual = service.quality.quality.get_quality_run(
            self.workspace_id, summary.run_id
        )
        self.assertEqual(actual.to_json(), expected.to_json())
        self.assertEqual(actual.content_hash, summary.content_hash)

    def test_odoo19_local_parity(self):
        self._assert_parity(19, OdooConnectionMode.LOCAL)

    def test_odoo19_remote_parity(self):
        self._assert_parity(19, OdooConnectionMode.REMOTE)

    def test_odoo20_local_parity(self):
        self._assert_parity(20, OdooConnectionMode.LOCAL)

    def test_odoo20_remote_parity(self):
        self._assert_parity(20, OdooConnectionMode.REMOTE)

    def test_failure_closes_scratch_and_keeps_review_unpublished(self):
        self.configure(257, 193)
        directories = []
        original = tempfile.TemporaryDirectory

        def temporary(*args, **kwargs):
            directory = original(*args, **kwargs)
            directories.append(Path(directory.name))
            return directory

        with (
            patch.object(preparation_quality_spill, "TemporaryDirectory", temporary),
            patch.object(
                self.context.preparation.quality.quality,
                "publish_quality_run",
                side_effect=ValueError("interrupted publication"),
            ),
            self.assertRaisesRegex(ValueError, "interrupted publication"),
        ):
            self.prepare()
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))
        self.assertIsNone(
            self.context.preparation.quality.current_summary(self.workspace_id)
        )


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_FINDING_PREPARATION_SCALE") == "1",
    "full preparation with many findings is opt-in",
)
class QualityFindingPreparationScaleTests(_FindingFixture):
    def test_54191_rows_with_32977_invalid_identifiers_publish_all_findings(self):
        self.configure(54_191, 32_977)
        started = perf_counter()
        cpu_started = process_time()
        with (
            fixtures._PeakWorkingSetSampler(psutil.Process()) as memory,
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as statements,
        ):
            self.prepare()
        prepare_seconds = perf_counter() - started
        cpu_seconds = process_time() - cpu_started
        summary = self.assert_counts(54_191, 32_977)
        repository = self.context.preparation.quality.quality
        with repository._connect(
            repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        ) as connection:
            finding_count, finding_bytes = connection.execute(
                "SELECT COUNT(*), SUM(octet_length(encode(issue_json))) FROM quality_issue WHERE run_id = ?",
                [summary.run_id],
            ).fetchone()
        self.assertEqual(finding_count, 3 * 32_977)
        print(
            json.dumps(
                {
                    "rows": 54_191,
                    "invalid_rows": 32_977,
                    "prepare_seconds": prepare_seconds,
                    "cpu_seconds": cpu_seconds,
                    "finding_count": finding_count,
                    "finding_json_bytes": finding_bytes,
                    "sampled_rss_bytes": memory.peak_bytes,
                    "connections": timing.connection_count,
                    "schema_checks": timing.schema_check_count,
                    "sql_statements": len(statements),
                    "content_hash": summary.content_hash,
                }
            ),
            flush=True,
        )
        # This fixture's validation, transformation, publication, quality and
        # normalization scopes require 5/6/10/12/8 opens and 3/5/7/9/6 checks.
        self.assertEqual(timing.connection_count, 5 + 6 + 10 + 12 + 8)
        self.assertEqual(timing.schema_check_count, 3 + 5 + 7 + 9 + 6)
        # Canonical pages scale with input rows; issue, quarantine and cause
        # transports also scale with findings. Allow eight statements per
        # thousand of each, plus fixed setup, while rejecting per-row SQL.
        self.assertLessEqual(
            len(statements), 550 + 8 * 55 + 8 * ((finding_count + 999) // 1000)
        )
