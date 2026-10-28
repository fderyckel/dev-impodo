from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueSeverity,
)
from impodo.domain.preparation.quality import (
    QualityCauseCount,
    QualityOutcomePolicy,
    QualityRuleFamily,
)
from impodo.web.presenters.summary import (
    _quality_issue_field_label_map,
    _quality_workflow_issue_summary,
    _preparation_limit_message,
    _summary_page_size,
)


class QualityCauseSummaryTests(unittest.TestCase):
    def test_groups_direct_and_inherited_causes_with_friendly_fields(self) -> None:
        direct_issue = SimpleNamespace(
            issue_id="direct",
            reason_code="SOURCE_TYPE_INVALID",
            message="The prepared value cannot be converted to integer.",
            affected_fields=("field:sequence",),
        )
        inherited_issue = SimpleNamespace(
            issue_id="inherited",
            reason_code="INCOMING_RELATIONSHIP_PARENT_SET_ASIDE",
            message="The linked BOM was already set aside.",
            affected_fields=("bom_id",),
        )
        item = SimpleNamespace(
            row=SimpleNamespace(dataset="bom_lines"),
            issues=(direct_issue, inherited_issue),
        )
        labels = {
            "bom_lines": {
                "field:sequence": "Operation number",
                "bom_id": "Bill of Materials",
            }
        }

        fields = _quality_issue_field_label_map((item,), labels)
        summary = _quality_workflow_issue_summary(
            "workspace:test",
            "quality-run",
            (
                QualityCauseCount(
                    dataset="bom_lines",
                    reason_code=direct_issue.reason_code,
                    message=direct_issue.message,
                    affected_fields=direct_issue.affected_fields,
                    affected_record_count=2,
                    family=QualityRuleFamily.BOUNDED_VALUES,
                    policy=QualityOutcomePolicy.BLOCK,
                    owner_label="Data manager",
                    affected_parent_count=1,
                    affected_child_count=2,
                    affected_sibling_count=3,
                ),
                QualityCauseCount(
                    dataset="bom_lines",
                    reason_code=inherited_issue.reason_code,
                    message=inherited_issue.message,
                    affected_fields=inherited_issue.affected_fields,
                    affected_record_count=5,
                    family=QualityRuleFamily.RELATIONSHIP_READINESS,
                    policy=QualityOutcomePolicy.QUARANTINE,
                    owner_label="Product data manager",
                ),
            ),
            labels,
            SimpleNamespace(
                datasets=(
                    SimpleNamespace(dataset_id="dataset:bom-lines", name="bom_lines"),
                )
            ),
            SimpleNamespace(
                definition=SimpleNamespace(
                    datasets=(
                        SimpleNamespace(
                            dataset_id="dataset:bom-lines",
                            target_model="mrp.bom.line",
                        ),
                    )
                )
            ),
            SimpleNamespace(
                models=(
                    SimpleNamespace(
                        name="mrp.bom.line",
                        fields=(
                            SimpleNamespace(
                                name="field:sequence",
                                label="Operation number",
                                type="integer",
                            ),
                            SimpleNamespace(
                                name="bom_id",
                                label="Bill of Materials",
                                type="many2one",
                            ),
                        ),
                    ),
                )
            ),
        )

        self.assertEqual(fields["direct"], ("Operation number",))
        self.assertEqual(fields["inherited"], ("Bill of Materials",))
        self.assertEqual(summary.must_fix_count, 1)
        self.assertEqual(summary.review_count, 1)
        self.assertEqual(
            sum(issue.affected_record_count or 0 for issue in summary.issues),
            7,
        )
        direct, inherited = summary.issues
        self.assertEqual(direct.severity, WorkflowIssueSeverity.MUST_FIX)
        self.assertEqual(direct.origin, WorkflowIssueOrigin.SOURCE_DATA)
        self.assertEqual(direct.affected_parent_count, 1)
        self.assertEqual(direct.affected_child_count, 2)
        self.assertEqual(direct.affected_sibling_count, 3)
        self.assertEqual(
            direct.correction_route,
            "/workspaces/workspace:test/mapping?mapping_dataset=0&field_query="
            "Operation+number#scalar-0-field:sequence",
        )
        self.assertEqual(
            inherited.origin,
            WorkflowIssueOrigin.INCOMING_RELATIONSHIP,
        )
        self.assertTrue(inherited.dependent_on_other_issue)
        self.assertEqual(
            inherited.correction_route,
            "/workspaces/workspace:test/mapping?mapping_dataset=0&relation_query="
            "Bill+of+Materials#relationship-0-bom_id",
        )


class SummaryPageSizeTests(unittest.TestCase):
    def test_accepts_only_the_four_bounded_display_sizes(self) -> None:
        self.assertEqual(
            tuple(_summary_page_size(str(size)) for size in (10, 20, 50, 100)),
            (10, 20, 50, 100),
        )
        self.assertEqual(_summary_page_size(None), 20)
        self.assertEqual(_summary_page_size("5000"), 20)
        self.assertEqual(_summary_page_size("invalid"), 20)


class PreparationLimitCopyTests(unittest.TestCase):
    def test_direct_limit_stays_in_source_and_field_rule_language(self) -> None:
        message = _preparation_limit_message(
            bounded_direct=True,
            supported_limit=50_000,
        )

        self.assertEqual(
            message,
            "With this source setup and these field rules, Impodo can safely "
            "prepare up to 50,000 rows in one project.",
        )
        self.assertNotIn("Polars", message)
        self.assertNotIn("Python", message)

    def test_related_limit_explains_the_lower_boundary(self) -> None:
        message = _preparation_limit_message(
            bounded_direct=False,
            supported_limit=25_000,
        )

        self.assertEqual(
            message,
            "This setup includes related or grouped source data, so Impodo "
            "can safely prepare up to 25,000 rows in one project.",
        )
        self.assertNotIn("DuckDB", message)
        self.assertNotIn("Parquet", message)


class SummaryCardLinkContractTests(unittest.TestCase):
    def test_each_card_targets_its_matching_filtered_row_section(self) -> None:
        template_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_summary.html"
        )
        template = template_path.read_text(encoding="utf-8")

        self.assertEqual(template.count('id="quality-rows"'), 1)
        self.assertEqual(template.count('id="readiness-rows"'), 1)
        for status in ("ready", "review", "quarantined", "blocked"):
            self.assertIn(
                f'href="?quality_status={status}#quality-rows"',
                template,
            )
        for status in ("create", "update", "unchanged", "attention"):
            self.assertIn(
                f'href="?status={status}#readiness-rows"',
                template,
            )


if __name__ == "__main__":
    unittest.main()
