"""Keep optional blank links separate from invalid paths and required values."""

from dataclasses import replace
from unittest import TestCase
from uuid import uuid4

from impodo.domain.staging.evaluator import (
    _DerivedReferencePlan,
    _normalize_derived_references,
)
from impodo.domain.workspace.derived_entities import DerivedEntityRule, HierarchicalLookupRule, HierarchyValuePolicy


class LookupBlankSourceTests(TestCase):
    def setUp(self):
        rule = DerivedEntityRule(
            str(uuid4()), "labels", "source", "choice", "x.any.label", "name",
            "fixture", parent_separator="/", blank_policy="quarantine",
        )
        self.plan = _DerivedReferencePlan(
            rule, "choice", "prepared", "x_link_id", optional_many2one=True,
            source_column_keys=("choice",),
        )

    def evaluate(self, value, plan=None):
        values = {"choice": value, "prepared": value}
        issues = _normalize_derived_references(
            values, (plan or self.plan,), dataset="joined", source_row=2
        )
        return values["prepared"], issues

    def test_optional_blanks_keep_record_without_an_additional_blank_policy(self):
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

    def test_required_and_list_relationships_keep_their_safeguard(self):
        for plan in (replace(self.plan, optional_many2one=False), replace(self.plan, required=True)):
            with self.subTest(plan=plan):
                _, issues = self.evaluate(None, plan)
                self.assertEqual([issue.code for issue in issues], ["DERIVED_REFERENCE_QUARANTINED"])

    def test_partly_blank_key_is_not_treated_as_an_empty_optional_reference(self):
        plan = replace(self.plan, source_column_keys=("choice", "scope"))
        values = {"choice": None, "scope": "Populated", "prepared": None}
        issues = _normalize_derived_references(values, (plan,), dataset="joined", source_row=2)
        self.assertEqual([issue.code for issue in issues], ["DERIVED_REFERENCE_QUARANTINED"])

    def test_partial_hierarchy_and_explicit_fallback_still_follow_their_rule(self):
        rule = HierarchicalLookupRule(str(uuid4()), "tree", "source", ("parent", "choice"),
            "x.any.label", "name", "fixture", HierarchyValuePolicy(mode="block"),
            missing_leaf="block", all_blank=HierarchyValuePolicy(mode="quarantine"))
        plan = replace(self.plan, rule=rule, source_column_keys=rule.source_level_column_keys)
        for parent, choice in (("Parent", None), (None, "Child")):
            values = {"parent": parent, "choice": choice, "prepared": choice}
            issues = _normalize_derived_references(values, (plan,), dataset="joined", source_row=2)
            self.assertTrue(issues)
        values = {"parent": None, "choice": None, "prepared": None}
        self.assertEqual(_normalize_derived_references(values, (plan,), dataset="joined", source_row=2), ())
        fixed = replace(plan, rule=replace(rule, all_blank=HierarchyValuePolicy(mode="fixed", value="Default")))
        self.assertEqual(_normalize_derived_references(values, (fixed,), dataset="joined", source_row=2), ())
        self.assertEqual(values["prepared"], "default")
