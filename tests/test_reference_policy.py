"""Verify one fail-closed policy governs every supporting Odoo reference."""

from __future__ import annotations

from dataclasses import replace
import unittest

from impodo.reference_keys import (
    REFERENCE_POLICY_HASH,
    GovernedReferenceRequest,
    ReferenceEvidenceKind,
    ReferencePolicyDenial,
    ReferenceReadPurpose,
    StandardReferenceFieldContract,
    authorize_governed_reference,
    authorize_supporting_match_probe,
    standard_reference_key,
)


class GovernedReferencePolicyTests(unittest.TestCase):
    @staticmethod
    def _country_request(**changes) -> GovernedReferenceRequest:
        request = GovernedReferenceRequest(
            parent_model="res.partner",
            relationship_field="country_id",
            relationship_type="many2one",
            relationship_model="res.country",
            related_model="res.country",
            key_fields=("code",),
            scope_fields=(),
            requested_fields=("code", "name"),
            purpose=ReferenceReadPurpose.PREFLIGHT,
        )
        return replace(request, **changes)

    def test_reviewed_country_is_authorized_without_schema_expansion(self):
        decision = authorize_governed_reference(
            self._country_request(),
            captured_fields=None,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(
            decision.evidence_kind,
            ReferenceEvidenceKind.REVIEWED_STANDARD,
        )
        self.assertEqual(decision.policy_hash, REFERENCE_POLICY_HASH)

    def test_compatible_capture_has_the_same_reviewed_meaning(self):
        country = standard_reference_key("res.country")
        assert country is not None

        decision = authorize_governed_reference(
            self._country_request(),
            captured_fields=country.field_contracts,
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(
            decision.evidence_kind,
            ReferenceEvidenceKind.REVIEWED_STANDARD,
        )

    def test_incompatible_explicit_capture_fails_closed(self):
        country = standard_reference_key("res.country")
        assert country is not None
        incompatible = tuple(
            replace(field, readonly=True) if field.name == "code" else field
            for field in country.field_contracts
        )

        decision = authorize_governed_reference(
            self._country_request(),
            captured_fields=incompatible,
        )

        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.denial,
            ReferencePolicyDenial.CAPTURED_METADATA_MISMATCH,
        )
        self.assertEqual(decision.affected_field, "code")

    def test_unreviewed_outside_model_and_extra_field_are_rejected(self):
        unreviewed = authorize_governed_reference(
            self._country_request(
                relationship_model="product.product",
                related_model="product.product",
                key_fields=("default_code",),
                requested_fields=("default_code",),
            ),
            captured_fields=None,
        )
        extra = authorize_governed_reference(
            self._country_request(requested_fields=("code", "active")),
            captured_fields=None,
        )

        self.assertEqual(unreviewed.denial, ReferencePolicyDenial.MODEL_NOT_REVIEWED)
        self.assertEqual(extra.denial, ReferencePolicyDenial.FIELD_NOT_ALLOWED)

    def test_wrong_relation_identity_unbounded_metadata_and_write_are_rejected(self):
        cases = (
            (
                self._country_request(relationship_model="res.lang"),
                ReferencePolicyDenial.RELATION_MISMATCH,
            ),
            (
                self._country_request(key_fields=("name",)),
                ReferencePolicyDenial.IDENTITY_NOT_GOVERNED,
            ),
            (
                self._country_request(all_fields=True),
                ReferencePolicyDenial.UNBOUNDED_METADATA,
            ),
            (
                self._country_request(include_unique_constraints=True),
                ReferencePolicyDenial.UNBOUNDED_METADATA,
            ),
            (
                self._country_request(write_use=True),
                ReferencePolicyDenial.WRITE_USE_FORBIDDEN,
            ),
        )

        for request, denial in cases:
            with self.subTest(denial=denial):
                decision = authorize_governed_reference(
                    request,
                    captured_fields=None,
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(decision.denial, denial)

    def test_captured_custom_reference_still_requires_a_governed_key(self):
        fields = (
            StandardReferenceFieldContract("external_code", "char", True, False),
        )
        request = self._country_request(
            relationship_model="x.region",
            related_model="x.region",
            key_fields=("external_code",),
            requested_fields=("external_code",),
        )

        denied = authorize_governed_reference(request, captured_fields=fields)
        accepted = authorize_governed_reference(
            replace(request, governed_key=True),
            captured_fields=fields,
        )

        self.assertEqual(
            denied.denial,
            ReferencePolicyDenial.IDENTITY_NOT_GOVERNED,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(
            accepted.evidence_kind,
            ReferenceEvidenceKind.CAPTURED_GOVERNED,
        )

    def test_many2one_name_probe_is_bounded_to_stage_three_choices(self):
        request = GovernedReferenceRequest(
            parent_model="product.template",
            relationship_field="uom_id",
            relationship_type="many2one",
            relationship_model="uom.uom",
            related_model="uom.uom",
            key_fields=("name",),
            scope_fields=(),
            requested_fields=("name",),
            purpose=ReferenceReadPurpose.MATCH_CHOICES,
            governed_key=True,
        )

        accepted = authorize_supporting_match_probe(request)
        wrong_field = authorize_supporting_match_probe(
            replace(request, requested_fields=("name", "category_id"))
        )
        wrong_purpose = authorize_supporting_match_probe(
            replace(request, purpose=ReferenceReadPurpose.PREFLIGHT)
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(
            accepted.evidence_kind,
            ReferenceEvidenceKind.BOUNDED_MATCH_PROBE,
        )
        self.assertFalse(wrong_field.accepted)
        self.assertFalse(wrong_purpose.accepted)


if __name__ == "__main__":
    unittest.main()
