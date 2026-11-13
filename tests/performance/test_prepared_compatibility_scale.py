"""Verify native findings survive bounded compatibility preparation at scale."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.performance import test_preparation_scale as fixtures
from tests.support.identity import resolve_local_actor


class _CompatibilityFixture(unittest.TestCase):
    setUp = fixtures.BoundedPreparationParityTests.setUp
    tearDown = fixtures.BoundedPreparationParityTests.tearDown

    def _prepare_project_and_evidence(self, **kwargs):
        result = fixtures.PreparationWorkflowScaleTests._prepare_project_and_evidence(
            self, **kwargs,
        )
        self.workspace_id = result[0]
        return result

    def _source_values_with_missing_identifiers(self, index, *args, **kwargs):
        values = self.original_source_values(index, *args, **kwargs)
        return ("", *values[1:]) if index < self.missing_identifiers else values

    def _assert_findings(self, rows: int, missing: int) -> None:
        service = self.context.preparation
        staging = service.staging.get_current_staging_summary(self.workspace_id)
        quality = service.quality.current_summary(self.workspace_id)
        normalization = service.normalization.current_summary(self.workspace_id)
        self.assertEqual(staging.total_rows, rows)
        self.assertEqual(quality.ready_count, rows - missing)
        self.assertEqual(quality.quarantined_count, missing)
        self.assertEqual(normalization.eligible_record_count, rows - missing)
        self.assertEqual(normalization.set_aside_record_count, missing)


class PreparedCompatibilityTests(_CompatibilityFixture):
    def test_findings_continue_above_legacy_python_limit_with_exact_evidence(self):
        self.original_source_values = fixtures._source_values
        self.missing_identifiers = 2
        with patch.object(fixtures, "_source_values", self._source_values_with_missing_identifiers):
            self._prepare_project_and_evidence(row_count=17, column_count=6, mapped_field_count=5)
        service = self.context.preparation
        revision = service.mappings.get_mapping_revision(self.workspace_id)
        physical = service.sources.get_source_selection(self.workspace_id)
        effective = service.sources.get_mapping_source_selection(self.workspace_id)
        expected = fixtures.preparation_module.stage_browser_mapping(
            service.workspaces.get(self.workspace_id), revision.definition,
            physical, effective, None,
            service.sources.get_source_catalogs(self.workspace_id), self.artifacts,
        )
        with (
            patch.object(fixtures.bounded_preparation_module,
                         "BOUNDED_DIRECT_BROWSER_EVALUATION_ROW_LIMIT", 16),
            patch.object(fixtures.preparation_module, "stage_browser_mapping",
                         side_effect=AssertionError("whole-run preparation fallback")),
        ):
            service.prepare(self.workspace_id, actor=resolve_local_actor(self.context))
        self._assert_findings(17, 2)
        actual = service.staging.get_current_staging_summary(self.workspace_id)
        self.assertEqual(actual.content_hash, expected.canonical_run.content_hash)


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1",
    "opt-in preparation scale qualification",
)
class PreparedCompatibilityScaleTests(_CompatibilityFixture):
    def _qualify(self, rows: int) -> None:
        self.original_source_values = fixtures._source_values
        self.missing_identifiers = 562
        with (
            patch.multiple(
                fixtures,
                PREPARATION_SCALE_WORKLOAD="products",
                PREPARATION_SCALE_ROWS=rows,
                PREPARATION_SCALE_COLUMNS=8,
                PREPARATION_SCALE_MAPPED_FIELDS=6,
                PREPARATION_SCALE_EFFECT_FIELDS=1,
                PREPARATION_SCALE_DIRTY=False,
            ),
            patch.object(fixtures, "_source_values", self._source_values_with_missing_identifiers),
        ):
            fixtures.PreparationWorkflowScaleTests.test_background_worker_releases_its_working_memory(
                self,
            )
        self._assert_findings(rows, 562)

    def test_reported_54_191_rows_with_562_missing_identifiers(self) -> None:
        self._qualify(54_191)

    def test_100_000_rows_with_missing_identifiers(self) -> None:
        self._qualify(100_000)
