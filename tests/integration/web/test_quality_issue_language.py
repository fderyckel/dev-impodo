"""Explain identifier failures using saved column names and evidence."""

from __future__ import annotations

import re
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Request
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.preparation.quality import (
    QualityCauseCount,
    QualityCauseImpact,
    QualityDisposition,
    QualityIssue,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityReviewItem,
    QualityReviewPage,
    QualityRowResult,
    QualityRuleFamily,
)
from impodo.web.presenters.summary import (
    _quality_field_label_map,
    _quality_issue_message_map,
    _quality_workflow_issue_summary,
    _render_quality_cause,
)
from impodo.web.presenters.user_help import DECISION_HELP_BY_ID
from tests.support.paths import REPOSITORY_ROOT


class QualityIssueLanguageTests(unittest.TestCase):
    def setUp(self):
        self.key = "column:join-group:bfec308b-eaed-5d41-9464-9004af1b6d6c"
        self.selection = SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    dataset_id="suppliers-id",
                    name="suppliers",
                    columns=(
                        SimpleNamespace(
                            stable_key=self.key, source_name="BusRelAccount"
                        ),
                        SimpleNamespace(stable_key="company", source_name="Company"),
                    ),
                ),
            )
        )
        self.revision = SimpleNamespace(
            definition=SimpleNamespace(
                datasets=(
                    DatasetMapping(
                        dataset_id="suppliers-id",
                        target_model="res.partner",
                        source_identity_column_keys=(self.key,),
                    ),
                )
            )
        )
        self.labels = _quality_field_label_map(self.revision, self.selection, None)
        self.causes = (
            QualityCauseCount(
                dataset="suppliers",
                reason_code="SOURCE_IDENTITY_INVALID",
                message="required value is empty",
                affected_fields=(self.key,),
                affected_record_count=150,
                family=QualityRuleFamily.BOUNDED_VALUES,
                policy=QualityOutcomePolicy.QUARANTINE,
                owner_label="Data manager",
                cause_key="sha256:" + "a" * 64,
            ),
            QualityCauseCount(
                dataset="suppliers",
                reason_code="SOURCE_IDENTITY_DUPLICATE",
                message="source identity (None,) occurs 150 times",
                affected_fields=(),
                affected_record_count=150,
                family=QualityRuleFamily.BOUNDED_VALUES,
                policy=QualityOutcomePolicy.QUARANTINE,
                owner_label="Data manager",
                cause_key="sha256:" + "b" * 64,
            ),
        )
        self.templates = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                    FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
                ]
            ),
            autoescape=True,
        )

    def summary(self, causes=None):
        return _quality_workflow_issue_summary(
            "workspace",
            "quality-run",
            causes or self.causes,
            self.labels,
            self.selection,
            self.revision,
            None,
        )

    def test_summary_explains_empty_identifier_and_source_only_duplicate_check(self):
        summary = self.summary()
        self.assertEqual((summary.must_fix_count, summary.review_count), (0, 2))
        by_code = {issue.code.split(":")[2]: issue for issue in summary.issues}
        missing = by_code["SOURCE_IDENTITY_INVALID"]
        duplicate = by_code["SOURCE_IDENTITY_DUPLICATE"]
        self.assertEqual(missing.cause, "Source row identifier is empty")
        self.assertIn("BusRelAccount) is empty", missing.explanation)
        self.assertIn("not with Odoo records", duplicate.explanation)
        self.assertIn(
            "150 source rows have an empty row identifier (BusRelAccount)",
            duplicate.explanation,
        )
        for issue in summary.issues:
            self.assertEqual(issue.affected_record_count, 150)
            self.assertEqual(issue.affected_fields, ("BusRelAccount",))
            self.assertIn("Unique row identifier", issue.recheck)
            self.assertIn(
                "step=tables&task=identity#source-identity-0", issue.correction_route
            )
            self.assertNotIn("field rules", issue.correction_label)
        html = self.templates.get_template("_workflow_issue_summary.html").render(
            workflow_issue_summary=summary,
        )
        visible = re.sub(r"<details\b.*?</details>", "", html, flags=re.S)
        self.assertNotIn("Your saved work", visible)
        self.assertNotIn("Impodo shows the cause", visible)
        self.assertIn("BusRelAccount", visible)

    def test_nonempty_duplicates_do_not_claim_all_values_are_empty(self):
        cause = replace(
            self.causes[1], message="source identity ('SUP42',) occurs 2 times"
        )
        issue = self.summary((cause,)).issues[0]
        self.assertIn("shared by more than one source row", issue.explanation)
        self.assertNotIn("identifier is empty", issue.explanation)
        self.assertNotIn("SUP42", issue.explanation)

    def test_grouped_duplicate_values_do_not_turn_one_empty_group_into_all_empty_rows(
        self,
    ):
        cause = replace(
            self.causes[1],
            message="source identity ('SUP42',) occurs 2 times",
            affected_record_count=2,
        )
        issue = self.summary((self.causes[1], cause)).issues[0]
        self.assertEqual(issue.affected_record_count, 152)
        self.assertNotIn("150 source rows have an empty", issue.explanation)
        self.assertIn("shared by more than one source row", issue.explanation)

    def test_composite_identifier_lists_each_selected_column(self):
        mapping = self.revision.definition.datasets[0]
        self.revision.definition.datasets = (
            replace(
                mapping,
                source_identity_column_keys=(self.key, "company"),
            ),
        )
        issue = self.summary((self.causes[1],)).issues[0]
        self.assertEqual(issue.affected_fields, ("BusRelAccount", "Company"))
        self.assertIn("BusRelAccount, Company", issue.explanation)

    def test_matching_value_failure_links_to_odoo_rule_instead_of_source_identifier(
        self,
    ):
        cause = replace(self.causes[0], affected_fields=("company",))
        issue = self.summary((cause,)).issues[0]
        self.assertEqual(issue.cause, "Odoo matching value is empty")
        self.assertIn("Odoo matching rule", issue.explanation)
        self.assertTrue(issue.correction_route.endswith("#target-identity-0"))

    def test_existing_external_id_finding_names_column_and_links_to_its_decision(self):
        self.labels["suppliers"][self.key] = "RecId"
        cause = replace(
            self.causes[0],
            reason_code="SOURCE_RECORD_IDENTITY_INVALID",
            message="The existing Odoo External ID is invalid",
        )
        issue = self.summary((cause,)).issues[0]
        self.assertEqual(issue.cause, "Review the Odoo External ID column")
        self.assertIn("RecId was selected as an Odoo External ID column", issue.explanation)
        self.assertIn("records being imported", issue.explanation)
        self.assertIn("No existing External ID column", issue.recheck)
        self.assertIn("when creating new records", issue.recheck)
        self.assertIn("prepare the complete data again", issue.recheck)
        self.assertIn("step=tables&task=identity#target-identity-0", issue.correction_route)
        self.assertEqual(issue.correction_label, "Review Suppliers External ID column")
        self.assertEqual(cause.message, "The existing Odoo External ID is invalid")
        html = self.templates.get_template("_workflow_issue_summary.html").render(
            workflow_issue_summary=self.summary((cause,)),
        )
        self.assertIn("No existing External ID column", html)
        self.assertNotIn("Open Suppliers field rules", html)

    def test_row_external_id_message_keeps_saved_support_evidence(self):
        self.labels["suppliers"][self.key] = "RecId"
        issue = QualityIssue(
            issue_id="sha256:" + "d" * 64,
            rule_id="sha256:" + "f" * 64,
            family=QualityRuleFamily.BOUNDED_VALUES,
            reason_code="SOURCE_RECORD_IDENTITY_INVALID",
            message="The existing Odoo External ID is invalid",
            dataset="suppliers",
            row_id="sha256:" + "c" * 64,
            source_row=2,
            affected_fields=(self.key,),
            policy=QualityOutcomePolicy.QUARANTINE,
            owner_role=QualityOwnerRole.DATA_MANAGER,
            owner_label="Data manager",
        )
        messages = _quality_issue_message_map(
            (SimpleNamespace(issues=(issue,)),), self.labels, self.selection, self.revision,
        )
        self.assertIn("RecId was selected", messages[issue.issue_id])
        self.assertIn("No existing External ID column", messages[issue.issue_id])
        self.assertNotIn(self.key, messages[issue.issue_id])
        self.assertEqual(issue.message, "The existing Odoo External ID is invalid")
        self.assertEqual(issue.rule_id, "sha256:" + "f" * 64)

    def test_external_id_guidance_uses_saved_labels_for_different_odoo_models(self):
        for model, column in (
            ("res.partner", "LegacyNumber"),
            ("mrp.bom.line", "RecId"),
            ("x.custom.record", "SourceKey"),
        ):
            with self.subTest(model=model, column=column):
                self.labels["suppliers"][self.key] = column
                self.revision.definition.datasets = (
                    replace(self.revision.definition.datasets[0], target_model=model),
                )
                cause = replace(
                    self.causes[0],
                    reason_code="SOURCE_RECORD_IDENTITY_INVALID",
                    message="The existing Odoo External ID is invalid",
                )
                issue = self.summary((cause,)).issues[0]
                self.assertIn(f"{column} was selected", issue.explanation)
                self.assertIn("No existing External ID column", issue.recheck)
                self.assertTrue(issue.correction_route.endswith("#target-identity-0"))
                self.assertNotIn(model, issue.explanation)

    def test_missing_source_identity_does_not_suggest_clearing_external_id_column(self):
        cause = replace(
            self.causes[0],
            reason_code="SOURCE_RECORD_IDENTITY_INVALID",
            message="Every source record identity column must have a value",
        )
        issue = self.summary((cause,)).issues[0]
        self.assertEqual(issue.explanation, cause.message)
        self.assertNotIn("No existing External ID column", issue.recheck)

    def test_detail_uses_column_names_and_keeps_raw_evidence_collapsed(self):
        row_id = "sha256:" + "c" * 64
        issues = tuple(
            QualityIssue(
                issue_id="sha256:" + digit * 64,
                rule_id="sha256:" + "f" * 64,
                family=cause.family,
                reason_code=cause.reason_code,
                message=cause.message,
                dataset=cause.dataset,
                row_id=row_id,
                source_row=695,
                affected_fields=cause.affected_fields,
                policy=cause.policy,
                owner_role=QualityOwnerRole.DATA_MANAGER,
                owner_label="Data manager",
            )
            for digit, cause in zip(("d", "e"), self.causes, strict=True)
        )
        review = QualityReviewPage(
            items=(
                QualityReviewItem(
                    row=QualityRowResult(
                        row_id=row_id,
                        dataset="suppliers",
                        source_row=695,
                        record_label="Row 695",
                        base_disposition=QualityDisposition.CANDIDATE,
                        effective_disposition=QualityDisposition.QUARANTINED,
                        issue_ids=tuple(sorted(issue.issue_id for issue in issues)),
                        requires_review=True,
                    ),
                    issues=issues,
                    cause_impact=QualityCauseImpact.DIRECT,
                    correction_route="Correct the source or field match, then check all rows again.",
                ),
            ),
            matching_count=150,
            page=1,
            page_count=8,
            selected_cause=self.causes[0],
        )
        context = MagicMock()
        context.navigation.get_for_workspace.return_value = SimpleNamespace(
            workspace_state=SimpleNamespace(),
            facts=SimpleNamespace(
                quality_run_id="quality-run",
                quality_staging_run_id="staging-run",
                staging_run_id="staging-run",
            ),
        )
        context.queries.get_quality_cause_review_page.return_value = review
        context.queries.get_mapping_source_selection.return_value = self.selection
        context.queries.get_mapping_revision.return_value = self.revision
        context.queries.get_odoo_schema_catalog.return_value = None
        with patch("impodo.web.presenters.summary._render") as render:
            _render_quality_cause(
                Request({"type": "http", "query_string": b"", "session": {}}),
                context,
                "workspace",
                self.causes[0].cause_key,
            )
        html = self.templates.get_template("workspace_quality_cause.html").render(
            **render.call_args.kwargs,
            workspace_id="workspace",
            decision_help_by_id=DECISION_HELP_BY_ID,
            url_for=lambda _name, *, path: f"/static{path}",
        )
        visible = re.sub(r"<details\b.*?</details>", "", html, flags=re.S)
        self.assertIn("Issue on this row", visible)
        self.assertIn("BusRelAccount) is empty", visible)
        self.assertIn("not with Odoo records", visible)
        self.assertIn("Records 1-20 of 150", visible)
        self.assertNotIn(self.key, visible)
        self.assertNotIn("Direct finding", visible)
        self.assertNotIn("(None,)", visible)
        self.assertNotIn("source or field match", visible)
        self.assertIn("source identity (None,) occurs 150 times", html)
        self.assertNotIn("<details open", html)
        context.queries.get_quality_cause_review_page.assert_called_once_with(
            "workspace",
            "quality-run",
            self.causes[0].cause_key,
            page=1,
            page_size=20,
        )
