"""Keep standalone comparison identities stable while incoming links carry shape."""

import unittest

from impodo.domain.shared.models import (
    BusinessReference,
    Classification,
    target_record_binding_hash,
)
from tests.support.practice_reference_cases import compared_reference_case


class IncomingReferenceShapeTests(unittest.TestCase):
    def test_flat_golden_manifest_preserves_prechange_business_hash(self):
        from tests.domain.execution.test_snapshot import _execution_fixture

        _input, result = _execution_fixture()
        # Independently recorded from base commit 1657554 with the same committed
        # CSV/profile/target snapshot. No helper implementation supplies this.
        self.assertEqual(
            result.semantic_hash,
            "sha256:a629a9196d75c053d9de9216a261b4c43a8217bfa272021378d27e52214ca5cf",
        )

    def test_standalone_scope_and_opaque_binding_remain_historical(self):
        _frozen, result = compared_reference_case()
        parent = next(item for item in result.decisions if item.dataset == "terms")
        self.assertEqual(
            parent.business_scope, (BusinessReference("res.company", ("North",)),)
        )
        company_evidence = next(
            item
            for item in result.reference_resolutions
            if item.reference.model == "res.company"
        )
        self.assertEqual(
            company_evidence.target_binding_hash,
            target_record_binding_hash("res.company", 9),
        )
        owner = next(
            item
            for item in result.decisions
            if item.classification is Classification.UPDATE
        )
        link = next(
            item.proposed
            for item in owner.differences
            if item.field == "property_payment_term_id"
        )
        self.assertEqual(link.model, "account.payment.term")
        self.assertEqual(link.key, ("Immediate",))
        self.assertEqual(link.target_fields, ("name",))
        self.assertEqual(link.target_scope_fields, ("company_id",))
        self.assertEqual(link.scope[0].key, ("North",))
        self.assertEqual(link.scope[0].target_fields, ("name",))
