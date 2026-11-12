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
from impodo.domain.preflight.reports import ReadinessIssueCount
from impodo.web.presenters.summary import (
    _quality_issue_field_label_map,
    _quality_workflow_issue_summary,
    _readiness_workflow_issue_summary,
    _preparation_limit_message,
    _summary_page_size,
)


class QualityCauseSummaryTests(unittest.TestCase):
    def test_synthetic_field_correction_uses_its_dataset_mapping(self) -> None:
        causes = tuple(
            QualityCauseCount(
                dataset=name, reason_code="SOURCE_TYPE_INVALID", message="Invalid number",
                affected_fields=("__impodo_scalar_0",), affected_record_count=1,
                family=QualityRuleFamily.BOUNDED_VALUES, policy=QualityOutcomePolicy.QUARANTINE,
                owner_label="Data manager",
            )
            for name in ("lines", "operations")
        )
        summary = _quality_workflow_issue_summary(
            "workspace:test", "quality-run", causes, {},
            SimpleNamespace(datasets=tuple(
                SimpleNamespace(dataset_id=name, name=name) for name in ("lines", "operations")
            )),
            SimpleNamespace(definition=SimpleNamespace(datasets=tuple(
                SimpleNamespace(
                    dataset_id=name, target_model="mrp.bom.line",
                    fields=(SimpleNamespace(target_field=field),), relationships=(),
                )
                for name, field in (("lines", "product_qty"), ("operations", "sequence"))
            ))),
            SimpleNamespace(models=(SimpleNamespace(
                name="mrp.bom.line", fields=tuple(
                    SimpleNamespace(name=name, label=label, type="float")
                    for name, label in (("product_qty", "Quantity"), ("sequence", "Sequence"))
                ),
            ),)),
        )
        routes = {issue.affected_record_types[0]: issue.correction_route for issue in summary.issues}
        self.assertEqual(routes["Lines"], "/workspaces/workspace:test/mapping?mapping_dataset=0&field_query=Quantity#scalar-0-product_qty")
        self.assertEqual(routes["Operations"], "/workspaces/workspace:test/mapping?mapping_dataset=1&field_query=Sequence#scalar-1-sequence")

    def test_numeric_capacity_advisory_routes_to_rule_and_odoo_schema(self) -> None:
        summary = _quality_workflow_issue_summary(
            "workspace:test",
            "quality-run",
            (
                QualityCauseCount(
                    dataset="bom_lines",
                    reason_code="TARGET_NUMERIC_PRECISION_LOSS",
                    message="The exact prepared value exceeds captured capacity.",
                    affected_fields=("product_qty",),
                    affected_record_count=4,
                    family=QualityRuleFamily.BOUNDED_VALUES,
                    policy=QualityOutcomePolicy.ADVISORY,
                    owner_label="Data manager",
                    target_digits=(16, 2),
                    required_decimal_places=6,
                    integral_overflow_count=1,
                    zeroed_positive_count=2,
                ),
            ),
            {"bom_lines": {"product_qty": "Quantity"}},
            SimpleNamespace(
                datasets=(
                    SimpleNamespace(
                        dataset_id="dataset:bom-lines",
                        name="bom_lines",
                    ),
                )
            ),
            SimpleNamespace(
                definition=SimpleNamespace(
                    datasets=(
                        SimpleNamespace(
                            dataset_id="dataset:bom-lines",
                            target_model="mrp.bom.line",
                            fields=(), relationships=(),
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
                                name="product_qty",
                                label="Quantity",
                                type="float",
                            ),
                        ),
                    ),
                )
            ),
        )

        self.assertEqual(summary.must_fix_count, 0)
        self.assertEqual(summary.review_count, 1)
        issue = summary.issues[0]
        self.assertEqual(issue.origin, WorkflowIssueOrigin.ODOO_SCHEMA)
        self.assertEqual(issue.target_digits, (16, 2))
        self.assertEqual(issue.required_decimal_places, 6)
        self.assertIn("field_query=Quantity", issue.correction_route)
        self.assertEqual(
            issue.secondary_correction_route,
            "/workspaces/workspace:test/schema",
        )

    def test_final_review_issue_routes_to_exact_persisted_decisions(self) -> None:
        summary = _readiness_workflow_issue_summary(
            "workspace:test",
            SimpleNamespace(
                run_id="readiness-run",
                datasets=(),
                issue_counts=(
                    ReadinessIssueCount(
                        dataset="products",
                        dataset_label="Products",
                        status="blocked",
                        technical_code="TARGET_REFERENCE_UNRESOLVED",
                        reason="An Odoo relationship has no usable business key.",
                        field="Category",
                        recommended_action=(
                            "Check the related Odoo record and its business key."
                        ),
                        affected_record_count=4,
                    ),
                ),
            ),
        )

        self.assertEqual(summary.must_fix_count, 1)
        self.assertEqual(summary.review_count, 0)
        issue = summary.issues[0]
        self.assertEqual(issue.origin, WorkflowIssueOrigin.ODOO_DATA)
        self.assertEqual(issue.owner, "Odoo data owner")
        self.assertEqual(issue.affected_record_count, 4)
        self.assertEqual(issue.affected_fields, ("Category",))
        self.assertIn("status=blocked", issue.correction_route)
        self.assertIn("dataset=products", issue.correction_route)
        self.assertIn(
            "issue=TARGET_REFERENCE_UNRESOLVED",
            issue.correction_route,
        )

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
                            fields=(), relationships=(),
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
    def test_ready_heading_does_not_repeat_row_accounting(self) -> None:
        template_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_summary.html"
        )
        template = template_path.read_text(encoding="utf-8")

        self.assertNotIn("Every source row is accounted for.", template)
        self.assertNotIn("unsafe records remain visible but set aside", template)

    def test_each_card_targets_its_matching_filtered_row_section(self) -> None:
        template_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "impodo"
            / "web"
            / "templates"
            / "workspace_summary.html"
        )
        partials = template_path.parent / "final_review"
        quality_cards = (partials / "_quality_cards.html").read_text(encoding="utf-8")
        comparison_cards = (partials / "_comparison_cards.html").read_text(encoding="utf-8")
        quality_rows = (partials / "_quality_rows.html").read_text(encoding="utf-8")
        comparison_rows = (partials / "_comparison_rows.html").read_text(encoding="utf-8")

        self.assertEqual(quality_rows.count('id="quality-rows"'), 1)
        self.assertEqual(comparison_rows.count('id="readiness-rows"'), 1)
        for status in ("ready", "review", "quarantined", "blocked"):
            self.assertIn(
                f'href="?step=prepared&amp;quality_status={status}#quality-rows"',
                quality_cards,
            )
        for status in ("create", "update", "unchanged", "attention"):
            view = "attention" if status == "attention" else "changes"
            self.assertIn(
                f'href="?step=results&amp;view={view}&amp;status={status}#readiness-rows"',
                comparison_cards,
            )
        self.assertIn('href="?step=results&amp;view=outside#records-outside-load"', comparison_cards)


if __name__ == "__main__":
    unittest.main()
