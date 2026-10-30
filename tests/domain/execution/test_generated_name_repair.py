"""Preview only exact, verified generated-name changes; never infer new IDs."""

from dataclasses import replace
from unittest import TestCase
from uuid import uuid4

from impodo.domain.execution.generated_name_repair import plan_generated_name_repairs
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityCandidate,
    DerivedEntityRule,
)


class GeneratedNameRepairTests(TestCase):
    def setUp(self):
        self.rule = DerivedEntityRule(
            str(uuid4()), "labels", "source", "choice", "x.label", "name", "fixture"
        )
        self.link = DerivedDatasetLink(
            "derived:labels",
            "source",
            "choice",
            "key",
            "display",
            None,
            "x.label",
            "name",
        )
        self.mapping = DatasetMapping(
            "derived:labels",
            "x.label",
            source_identity_column_keys=("key",),
            target_identity=(IdentityComponentMapping(("key",), ("name",)),),
        )
        self.candidates = tuple(
            DerivedEntityCandidate(
                key, f"fixture.{key}", key, name, None, (name,), 1, False
            )
            for key, name in (("alpha", "ALPHA"), ("beta", "Beta"))
        )
        self.attempts = tuple(
            ExecutionRowAttempt(
                f"row-{index}",
                "labels",
                index + 2,
                "x.label",
                "CREATE",
                ("name",),
                f"fixture.{index}",
                status=ExecutionRowStatus.COMMITTED,
                odoo_id=index + 91,
            )
            for index in range(2)
        )

    def plan(self, **changes):
        values = dict(
            mapping=self.mapping,
            link=self.link,
            rule=self.rule,
            candidates=self.candidates,
            attempts=self.attempts,
            verified_row_ids=frozenset(row.row_id for row in self.attempts),
        )
        return plan_generated_name_repairs(**(values | changes))

    def test_preserves_mixed_spelling_and_exact_receipts_instead_of_uppercasing(self):
        repairs = self.plan()
        self.assertEqual(
            [(row.odoo_id, row.original_name) for row in repairs],
            [(91, "ALPHA"), (92, "Beta")],
        )
        self.assertEqual(
            [row.external_id for row in repairs], ["fixture.0", "fixture.1"]
        )

    def test_unverified_missing_duplicate_or_failed_receipt_cannot_generate_a_repair(
        self,
    ):
        for change in (
            {"verified_row_ids": frozenset()},
            {"attempts": self.attempts[:1]},
            {"attempts": (self.attempts[0], replace(self.attempts[1], odoo_id=91))},
            {
                "attempts": (
                    self.attempts[0],
                    replace(self.attempts[1], status=ExecutionRowStatus.FAILED),
                )
            },
            {
                "candidates": (
                    replace(self.candidates[0], requires_alias_review=True),
                    self.candidates[1],
                )
            },
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.plan(**change)

    def test_already_correct_mapping_is_not_silently_retargeted(self):
        mapping = replace(
            self.mapping,
            target_identity=(IdentityComponentMapping(("display",), ("name",)),),
        )
        with self.assertRaises(ValueError):
            self.plan(mapping=mapping)
