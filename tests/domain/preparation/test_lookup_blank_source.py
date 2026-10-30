"""Keep optional blank links separate from invalid paths and required values."""

from dataclasses import replace
from unittest import TestCase
from uuid import uuid4

from impodo.domain.staging.evaluator import (
    _DerivedReferencePlan,
    _normalize_derived_references,
)
from impodo.domain.workspace.derived_entities import DerivedEntityRule


class LookupBlankSourceTests(TestCase):
    def setUp(self):
        rule = DerivedEntityRule(
            str(uuid4()), "labels", "source", "choice", "x.any.label", "name",
            "fixture", parent_separator="/", blank_policy="quarantine",
        )
        self.plan = _DerivedReferencePlan(
            rule, "choice", "prepared", "x_link_id", blank_source_action="leave_blank"
        )

    def evaluate(self, value, plan=None):
        values = {"choice": value, "prepared": value}
        issues = _normalize_derived_references(
            values, (plan or self.plan,), dataset="joined", source_row=2
        )
        return values["prepared"], issues

    def test_explicit_optional_blanks_keep_record_but_do_not_create_lookup_member(self):
        for value in (None, "", " \t\u3000 "):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(value), (None, ()))

    def test_populated_values_and_invalid_paths_still_follow_source_rules(self):
        for value, expected in ((False, "false"), (0, "0"), (" Retail / North ", "retail / north")):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(value), (expected, ()))
        prepared, issues = self.evaluate("Retail // North")
        self.assertIsNone(prepared)
        self.assertEqual([issue.code for issue in issues], ["DERIVED_REFERENCE_QUARANTINED"])

    def test_existing_source_policy_and_required_fields_keep_their_safeguard(self):
        for plan in (replace(self.plan, blank_source_action="source_rules"), replace(self.plan, required=True)):
            with self.subTest(plan=plan):
                _, issues = self.evaluate(None, plan)
                self.assertEqual([issue.code for issue in issues], ["DERIVED_REFERENCE_QUARANTINED"])
