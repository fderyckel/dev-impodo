"""Keep missing records distinct from unavailable fields and unverified reads."""

from unittest import TestCase

from impodo.domain.preflight.attention import attention_guidance


class AttentionGuidanceTests(TestCase):
    def test_missing_field_and_incomplete_evidence_do_not_claim_a_record_is_absent(
        self,
    ):
        missing_record, _, _ = attention_guidance("REFERENCE_NOT_FOUND")
        unavailable_field, action, location = attention_guidance("TARGET_FIELD_UNKNOWN")
        unverified, check, _ = attention_guidance("TARGET_REFERENCE_UNRESOLVED")
        self.assertEqual(
            missing_record, "No related record matched the checked Odoo data"
        )
        self.assertEqual(unavailable_field, "Selected Odoo field is unavailable")
        self.assertIn("Refresh Odoo data", action)
        self.assertEqual(location, "Odoo data, then Match data")
        self.assertEqual(unverified, "Could not verify the related record")
        self.assertIn("Do not create a replacement", check)

    def test_unfamiliar_findings_still_explain_where_to_review_the_saved_result(self):
        problem, action, location = attention_guidance("FUTURE_CHECK")
        self.assertEqual(problem, "Saved check needs attention")
        self.assertIn("saved finding", action)
        self.assertIn("Final review", location)
