"""Keep company-list reads bounded without assuming company names are unique."""

import unittest
from dataclasses import replace

from impodo.domain.workspace.reference_keys import (
    READABLE_REFERENCE_POLICY_HASHES,
    GovernedReferenceRequest,
    ReferenceEvidenceKind,
    ReferenceReadPurpose,
    StandardReferenceFieldContract,
    authorize_governed_reference,
    authorize_supporting_match_probe,
    reference_policy_hash,
    standard_reference_key,
    supporting_reference_candidate,
)


class CompanyReferencePolicyTests(unittest.TestCase):
    def test_previous_policies_remain_readable_but_are_not_current(self):
        previous = {
            "sha256:2c0d36b4830ff458df6b501f18aca56b6d54833700fd8641096a70ea2c8fca25",
            "sha256:15268eed0edc1c73d11e8d75a8fc98c6d62f763efb770b9bc4707338d7cf6233",
        }
        self.assertTrue(previous <= READABLE_REFERENCE_POLICY_HASHES)
        self.assertTrue(
            previous.isdisjoint({reference_policy_hash(19), reference_policy_hash(20)})
        )

    def request(self, major):
        return GovernedReferenceRequest(
            parent_model="account.account",
            relationship_field="company_ids",
            relationship_type="many2many",
            relationship_model="res.company",
            related_model="res.company",
            key_fields=("name",),
            scope_fields=(),
            requested_fields=("name",),
            purpose=ReferenceReadPurpose.MATCH_CHOICES,
            odoo_major_version=major,
            governed_key=True,
        )

    def test_both_majors_allow_only_a_verified_company_name_probe(self):
        for major in (19, 20):
            with self.subTest(major=major):
                request = self.request(major)
                candidate = supporting_reference_candidate(
                    "res.company", odoo_major_version=major
                )
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.odoo_major_version, major)
                self.assertEqual(candidate.key_fields, ("name",))
                self.assertIsNone(
                    standard_reference_key("res.company", odoo_major_version=major)
                )
                for fields in (
                    None,
                    (StandardReferenceFieldContract("name", "char", True, False),),
                ):
                    result = authorize_supporting_match_probe(
                        request, captured_fields=fields
                    )
                    self.assertTrue(result.accepted)
                    self.assertEqual(
                        result.evidence_kind, ReferenceEvidenceKind.BOUNDED_MATCH_PROBE
                    )
                    self.assertEqual(result.policy_hash, reference_policy_hash(major))
                self.assertFalse(
                    authorize_governed_reference(request, captured_fields=None).accepted
                )

    def test_company_probe_does_not_authorize_other_lists_or_wider_reads(self):
        for major in (19, 20):
            request = self.request(major)
            for changes in (
                {"related_model": "x.company", "relationship_model": "x.company"},
                {
                    "related_model": "product.product",
                    "relationship_model": "product.product",
                    "key_fields": ("default_code",),
                    "requested_fields": ("default_code",),
                },
                {"relationship_model": "res.partner"},
                {"relationship_type": "one2many"},
                {"requested_fields": ("name", "id")},
                {"key_fields": ("id",), "requested_fields": ("id",)},
                {
                    "scope_fields": ("company_id",),
                    "requested_fields": ("name", "company_id"),
                },
                {"purpose": ReferenceReadPurpose.PREFLIGHT},
                {"purpose": ReferenceReadPurpose.RECIPE_APPLICATION},
                {"governed_key": False},
                {"all_fields": True},
                {"include_unique_constraints": True},
                {"write_use": True},
                {"odoo_major_version": 21},
            ):
                with self.subTest(major=major, changes=changes):
                    self.assertFalse(
                        authorize_supporting_match_probe(
                            replace(request, **changes)
                        ).accepted
                    )
            wrong_type = (
                StandardReferenceFieldContract("name", "integer", True, False),
            )
            self.assertFalse(
                authorize_supporting_match_probe(
                    request, captured_fields=wrong_type
                ).accepted
            )
            self.assertFalse(
                authorize_governed_reference(
                    request, captured_fields=wrong_type
                ).accepted
            )

    def test_captured_governed_product_lists_keep_their_existing_read_contract(self):
        for major in (19, 20):
            with self.subTest(major=major):
                request = replace(
                    self.request(major),
                    related_model="product.product",
                    relationship_model="product.product",
                    key_fields=("default_code",),
                    requested_fields=("default_code",),
                )
                fields = (
                    StandardReferenceFieldContract(
                        "default_code", "char", False, False
                    ),
                )
                self.assertTrue(
                    authorize_governed_reference(
                        request, captured_fields=fields
                    ).accepted
                )
                self.assertFalse(
                    authorize_supporting_match_probe(
                        request, captured_fields=fields
                    ).accepted
                )
