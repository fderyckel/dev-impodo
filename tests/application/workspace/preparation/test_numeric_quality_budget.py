"""Preserve numeric quality evidence while bounding its retained consumers."""

from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from impodo.application.workspace.preparation.bounded_quality import (
    build_bounded_quality_run,
)
from impodo.domain.preparation.quality import (
    QualityError,
    default_quality_ruleset,
    evaluate_quality,
)
from impodo.domain.preparation.quality_budget import QualityFindingBudget
from impodo.domain.preparation.staging_contracts import StagingDisposition
from impodo.domain.target_numeric_precision import PreparedNumericField
from tests.domain.preparation import test_quality as fixtures


class _BudgetRows(fixtures._IndexedTestRows):
    """Expose exact indexed findings and one shared retained-evidence budget."""

    def bounded_quality_index(self, physical_rows, *, numeric_fields=()):
        precision_rows = []
        for ordinal, row in enumerate(self):
            index = fixtures._IndexedTestRows((row,)).bounded_quality_index(
                {row.lineage.physical_dataset_id: (row.source_row,)},
                numeric_fields=numeric_fields,
            )
            for raw in index["precision_rows"]:
                precision_rows.append((ordinal, *raw[1:]))
        return {
            "row_count": len(self),
            "disposition_counts": {
                disposition.value: len(self)
                if disposition is StagingDisposition.CANDIDATE
                else 0
                for disposition in StagingDisposition
            },
            "issue_rows": (),
            "collisions": (),
            "exception_rows": (),
            "precision_rows": tuple(precision_rows),
            "finding_budget": self.budget,
        }


class NumericQualityBudgetTests(TestCase):
    """Cover consumer memory through complete immutable quality construction."""

    def _case(self, *, limit_bytes=64 * 1024 * 1024, used_bytes=0):
        workspace = fixtures._workspace_state()
        rows = tuple(
            replace(
                fixtures._canonical_row(
                    token,
                    source_row,
                    source_identity=(token,),
                    target_identity=(token,),
                ),
                proposed_values={"name": token, "credit_limit": Decimal("0.003")},
            )
            for token, source_row in (("a", 2), ("b", 3))
        )
        staging = fixtures._staging(workspace.workspace_id, rows)
        indexed = _BudgetRows(rows)
        indexed.budget = QualityFindingBudget(limit_bytes, used_bytes)
        stored = replace(fixtures._stored_staging(staging), rows=indexed)
        ruleset = default_quality_ruleset(
            workspace_id=workspace.workspace_id,
            mapping_hash=fixtures.MAPPING_HASH,
            schema_hash=fixtures.SCHEMA_HASH,
            datasets=("contacts",),
        )
        arguments = {
            "workspace_state": workspace,
            "physical_rows": {"dataset:contacts": (2, 3)},
            "ruleset": ruleset,
            "published_staging_content_hash": staging.content_hash,
            "numeric_fields": (
                PreparedNumericField(
                    "contacts", "res.partner", "credit_limit", (16, 2)
                ),
            ),
        }
        return staging, stored, indexed.budget, arguments

    def test_complete_quality_payload_and_hash_match_the_authoritative_evaluator(self):
        staging, stored, budget, arguments = self._case()
        expected = evaluate_quality(staging=staging, **arguments)

        actual = build_bounded_quality_run(staging=stored, **arguments)
        materialized = fixtures._materialized_bounded_quality(actual)

        self.assertEqual(materialized.to_json(), expected.to_json())
        self.assertEqual(materialized.content_hash, expected.content_hash)
        self.assertEqual(len(actual.issues), 2)
        self.assertGreater(budget.used_bytes, 0)
        self.assertNotIn("0.003", repr(actual.issues))

    def test_raw_projection_and_consumer_maps_share_the_same_allowance(self):
        _staging, stored, budget, arguments = self._case(
            limit_bytes=64 * 1024 * 1024,
            used_bytes=64 * 1024 * 1024 - 1,
        )

        with self.assertRaisesRegex(QualityError, "bounded review memory budget"):
            build_bounded_quality_run(staging=stored, **arguments)

        self.assertEqual(budget.used_bytes, budget.limit_bytes - 1)
        self.assertEqual(len(stored.rows), 2)

    def test_numeric_issue_payload_is_charged_before_final_publication(self):
        _staging, stored, _budget, arguments = self._case(limit_bytes=1400)

        with self.assertRaisesRegex(QualityError, "bounded review memory budget"):
            build_bounded_quality_run(staging=stored, **arguments)

        self.assertEqual(
            tuple(row.proposed_values["credit_limit"] for row in stored.rows),
            (Decimal("0.003"), Decimal("0.003")),
        )
