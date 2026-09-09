from __future__ import annotations

from dataclasses import replace
import unittest

from impodo.domain.mapping.contracts import TargetFieldHandling
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    VerifiedCreateDefaultAction,
    decide_verified_create_default,
    evaluate_create_field,
    supports_create_default_capture,
)
from impodo.domain.workspace.contracts import SchemaField


class CreateFieldPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.field = SchemaField(
            name="group_on",
            label="Grouping",
            type="selection",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(("default", "Expected Date"),),
        )

    def test_verified_default_waits_for_confirmation(self) -> None:
        field = replace(
            self.field,
            create_default_present=True,
            create_default_value="default",
        )

        available = evaluate_create_field(
            field,
            provided=False,
            handling=None,
        )
        confirmed = evaluate_create_field(
            field,
            provided=False,
            handling=TargetFieldHandling.ODOO_DEFAULT,
        )

        self.assertEqual(available.coverage, CreateFieldCoverage.DEFAULT_AVAILABLE)
        self.assertEqual(confirmed.coverage, CreateFieldCoverage.DEFAULT_CONFIRMED)

    def test_unverified_default_and_readonly_write_fail_closed(self) -> None:
        unverified = evaluate_create_field(
            self.field,
            provided=False,
            handling=TargetFieldHandling.ODOO_DEFAULT,
        )
        readonly = evaluate_create_field(
            replace(self.field, readonly=True),
            provided=True,
            handling=None,
        )

        self.assertEqual(
            unverified.coverage,
            CreateFieldCoverage.DEFAULT_UNVERIFIED,
        )
        self.assertEqual(
            readonly.coverage,
            CreateFieldCoverage.READONLY_CONFLICT,
        )

    def test_unprovided_computed_or_related_field_is_odoo_managed(self) -> None:
        for field in (
            replace(self.field, computed=True),
            replace(self.field, related=True),
        ):
            with self.subTest(field=field):
                assessment = evaluate_create_field(
                    field,
                    provided=False,
                    handling=None,
                )

                self.assertEqual(
                    assessment.coverage,
                    CreateFieldCoverage.ODOO_MANAGED_CONFIRMED,
                )

    def test_required_many2one_can_use_target_bound_odoo_default(self) -> None:
        field = replace(
            self.field,
            type="many2one",
            relation="account.account",
            selection=(),
            create_default_present=True,
            create_default_value=42,
        )

        self.assertTrue(supports_create_default_capture(field))
        self.assertEqual(
            evaluate_create_field(
                field,
                provided=False,
                handling=None,
            ).coverage,
            CreateFieldCoverage.DEFAULT_AVAILABLE,
        )
        self.assertIs(
            decide_verified_create_default(field).action,
            VerifiedCreateDefaultAction.REQUIRE_REVIEW,
        )

    def test_non_company_scalar_default_can_be_applied_automatically(self) -> None:
        field = replace(
            self.field,
            type="char",
            selection=(),
            company_dependent=False,
            create_default_present=True,
            create_default_value="AUTO",
        )

        decision = decide_verified_create_default(field)

        self.assertIs(
            decision.action,
            VerifiedCreateDefaultAction.APPLY_AUTOMATICALLY,
        )
        self.assertIn("exact target", decision.reason)

    def test_context_sensitive_defaults_retain_review(self) -> None:
        cases = (
            (replace(self.field, create_default_present=True), "workflow"),
            (
                replace(
                    self.field,
                    type="monetary",
                    selection=(),
                    create_default_present=True,
                    create_default_value=12.5,
                ),
                "amount",
            ),
            (
                replace(
                    self.field,
                    type="char",
                    selection=(),
                    company_dependent=True,
                    create_default_present=True,
                    create_default_value="UC",
                ),
                "company",
            ),
            (
                replace(
                    self.field,
                    type="char",
                    selection=(),
                    company_dependent=None,
                    create_default_present=True,
                    create_default_value="UC",
                ),
                "prove",
            ),
        )
        for field, reason_fragment in cases:
            with self.subTest(field_type=field.type, company=field.company_dependent):
                decision = decide_verified_create_default(field)
                self.assertIs(
                    decision.action,
                    VerifiedCreateDefaultAction.REQUIRE_REVIEW,
                )
                self.assertIn(reason_fragment, decision.reason)


if __name__ == "__main__":
    unittest.main()
