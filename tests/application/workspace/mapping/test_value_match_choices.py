"""Keep dialog coverage aligned with shared relationship precedence."""

from collections import Counter
from unittest import TestCase

from impodo.application.workspace.mapping.value_match_choices import (
    IncomingValueChoices,
    combined_value_choices,
)


class CombinedValueChoiceTests(TestCase):
    def choices(
        self,
        source,
        incoming,
        targets,
        *,
        model="account.payment.term",
        ambiguous=(),
        normalize=False,
    ):
        return combined_value_choices(
            IncomingValueChoices("terms", "Payment terms", incoming, normalize),
            tuple({"value": value, "count": 1} for value in source),
            tuple({"value": value, "label": value} for value in targets),
            ambiguous,
            owner_dataset_id="vendors",
            target_field="property_supplier_payment_term_id",
            related_model=model,
            scoped_key=False,
        )["relationship_resolutions"]

    def test_mixed_existing_new_missing_and_duplicate_terms(self):
        rows = self.choices(
            ("EXISTING", "NEW", "MISSING", "DUPLICATE"),
            {("EXISTING",): 1, ("NEW",): 1, ("DUPLICATE",): 2},
            ("EXISTING",),
        )
        self.assertEqual(
            {row["source_value"]: row["status"] for row in rows},
            {
                "EXISTING": "target",
                "NEW": "incoming",
                "MISSING": "missing",
                "DUPLICATE": "ambiguous",
            },
        )

    def test_duplicate_odoo_key_blocks_fallback_even_when_omitted_from_choices(self):
        rows = self.choices(("DUP",), {("DUP",): 1}, (), ambiguous=("DUP",))
        self.assertEqual(rows[0]["status"], "ambiguous")

    def test_payment_terms_reuse_one_case_match_but_other_models_require_review(self):
        for model, expected in (
            ("account.payment.term", "target"),
            ("x.category", "case_only"),
        ):
            with self.subTest(model=model):
                rows = self.choices(
                    ("NET30",), {("NET30",): 1}, ("Net30",), model=model
                )
                self.assertEqual(rows[0]["status"], expected)
                self.assertEqual(
                    rows[0]["target_value"], "Net30" if expected == "target" else ""
                )

    def test_case_ambiguity_does_not_fall_back_or_choose_the_first_target(self):
        rows = self.choices(("NET30",), {("NET30",): 1}, ("Net30", "net30"))
        self.assertEqual(rows[0]["status"], "ambiguous")

    def test_generated_keys_follow_preparation_normalization(self):
        rows = self.choices(
            ("00FM05", "NET30"),
            {("00fm05",): 1, ("net30",): 1},
            ("NET30",),
            normalize=True,
        )
        self.assertEqual(
            Counter(row["status"] for row in rows), {"incoming": 1, "target": 1}
        )

    def test_exact_target_wins_over_case_ambiguity_and_duplicate_incoming_rows(self):
        rows = self.choices(("NET30",), {("NET30",): 2}, ("NET30", "Net30"))
        self.assertEqual(rows[0]["status"], "target")
