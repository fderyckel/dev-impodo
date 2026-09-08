from __future__ import annotations

from dataclasses import replace
import unittest

from impodo.domain.mapping.contracts import TargetFieldHandling
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
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


if __name__ == "__main__":
    unittest.main()
