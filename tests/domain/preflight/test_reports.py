from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from impodo.domain.preflight.reports import (
    ReadinessDataset,
    ReadinessReport,
    readiness_issue_counts,
)
from impodo.domain.shared.models import Classification, Decision, Issue, Severity


class ReadinessIssueCountTests(unittest.TestCase):
    def test_aggregates_every_blocker_and_ambiguous_match_by_complete_run(self) -> None:
        decisions = (
            Decision(
                dataset="products",
                source_row=2,
                business_identity=("P-001",),
                business_scope=(),
                classification=Classification.BLOCKED,
                target_match_count=0,
                source_trace_id="sha256:" + "1" * 64,
                issues=(
                    Issue(
                        code="SOURCE_REQUIRED_VALUE_MISSING",
                        message="name is required",
                        field="name",
                    ),
                    Issue(
                        code="REFERENCE_NOT_FOUND",
                        message="category is missing",
                        field="categ_id",
                    ),
                    Issue(
                        code="REVIEW_ONLY",
                        message="advisory",
                        severity=Severity.WARNING,
                    ),
                ),
            ),
            Decision(
                dataset="products",
                source_row=3,
                business_identity=("P-002",),
                business_scope=(),
                classification=Classification.BLOCKED,
                target_match_count=0,
                source_trace_id="sha256:" + "2" * 64,
                issues=(
                    Issue(
                        code="SOURCE_REQUIRED_VALUE_MISSING",
                        message="name is required",
                        field="name",
                    ),
                ),
            ),
            Decision(
                dataset="products",
                source_row=4,
                business_identity=("P-003",),
                business_scope=(),
                classification=Classification.AMBIGUOUS,
                target_match_count=2,
                source_trace_id="sha256:" + "3" * 64,
            ),
        )

        counts = readiness_issue_counts(
            decisions,
            {"products": "Products"},
            {
                ("products", "name"): "Product name",
                ("products", "categ_id"): "Category",
            },
        )

        self.assertEqual(len(counts), 3)
        required = next(
            item
            for item in counts
            if item.technical_code == "SOURCE_REQUIRED_VALUE_MISSING"
        )
        relationship = next(
            item for item in counts if item.technical_code == "REFERENCE_NOT_FOUND"
        )
        ambiguous = next(
            item
            for item in counts
            if item.technical_code == "TARGET_IDENTITY_AMBIGUOUS"
        )
        self.assertEqual(required.affected_record_count, 2)
        self.assertEqual(required.field, "Product name")
        self.assertEqual(relationship.affected_record_count, 1)
        self.assertEqual(relationship.field, "Category")
        self.assertEqual(ambiguous.status, "needs_review")
        self.assertEqual(ambiguous.affected_record_count, 1)
        self.assertNotIn("REVIEW_ONLY", {item.technical_code for item in counts})

    def test_older_report_without_issue_counts_remains_readable(self) -> None:
        report = ReadinessReport(
            run_id="00000000-0000-4000-8000-000000000001",
            workspace_id="workspace:test",
            mapping_id="mapping:test",
            mapping_version=1,
            mapping_content_hash="sha256:mapping",
            staging_run_id="00000000-0000-4000-8000-000000000002",
            staging_content_hash="sha256:staging",
            quality_run_id="00000000-0000-4000-8000-000000000003",
            quality_content_hash="sha256:quality",
            normalization_run_id="00000000-0000-4000-8000-000000000004",
            normalization_content_hash="sha256:normalization",
            normalization_lifecycle_version=1,
            eligible_dataset_hash="sha256:eligible",
            frozen_input_hash="sha256:frozen",
            requirement_plan_hash="sha256:requirements",
            metadata_snapshot_hash="sha256:metadata",
            record_snapshot_hash="sha256:records",
            result_hash="sha256:result",
            manifest_hash="sha256:manifest",
            target_hash="sha256:target",
            target_database="odoo",
            target_odoo_version="19.0",
            target_snapshot_at="2026-10-29T10:00:00+00:00",
            target_module_versions={},
            checked_at=datetime(2026, 10, 29, 10, tzinfo=UTC),
            checked_by="Data manager",
            datasets=(
                ReadinessDataset(
                    dataset="products",
                    label="Products",
                    target_model="product.product",
                    total=1,
                    ready=0,
                    needs_review=0,
                    blocked=1,
                ),
            ),
            rows=(),
        )
        payload = json.loads(report.to_json())
        del payload["issue_counts"]

        restored = ReadinessReport.from_json(json.dumps(payload))

        self.assertEqual(restored.issue_counts, ())
        self.assertEqual(restored.blocked_count, 1)


if __name__ == "__main__":
    unittest.main()
