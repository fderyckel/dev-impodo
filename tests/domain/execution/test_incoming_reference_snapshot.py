"""Preserve actual and historical incoming links through saved execution rows."""

import unittest
from dataclasses import replace

from impodo.domain.shared.models import (
    BusinessReference,
    Classification,
    LogicalReference,
    portable_value,
    restore_portable_value,
    target_record_binding_hash,
)
from tests.support.practice_reference_cases import (
    compared_reference_case as _comparison,
)
from tests.support.practice_reference_cases import legacy_reference as _legacy
from tests.support.practice_reference_cases import saved_reference_snapshot as _snapshot


class IncomingReferenceSnapshotTests(unittest.TestCase):
    def test_actual_scoped_comparison_preserves_new_term_for_create_and_update(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    frozen, result = _comparison(major, mode)
                    self.assertEqual(
                        result.counts,
                        {
                            "CREATE": 2,
                            "UPDATE": 1,
                            "UNCHANGED": 0,
                            "BLOCKED": 0,
                            "AMBIGUOUS": 0,
                        },
                    )
                    # Standalone scope evidence keeps the historical representation.
                    term = next(
                        item for item in result.decisions if item.dataset == "terms"
                    )
                    self.assertEqual(
                        term.business_scope,
                        (BusinessReference("res.company", ("North",)),),
                    )
                    update = next(
                        item
                        for item in result.decisions
                        if item.classification is Classification.UPDATE
                    )
                    proposed = next(
                        item.proposed
                        for item in update.differences
                        if item.field == "property_payment_term_id"
                    )
                    self.assertEqual(proposed.scope[0].target_fields, ("name",))
                    # Real current comparison and two historical shapes must keep
                    # the same business dependency after snapshot serialization.
                    for shape in ("current", "old-shallow", "legacy"):
                        decisions = []
                        for decision in result.decisions:
                            differences = []
                            for difference in decision.differences:
                                value = difference.proposed
                                if (
                                    isinstance(value, BusinessReference)
                                    and difference.field == "property_payment_term_id"
                                ):
                                    if shape == "legacy":
                                        value = _legacy(value)
                                    elif shape == "old-shallow":
                                        value = replace(
                                            value,
                                            key=tuple(
                                                _legacy(item) for item in value.key
                                            ),
                                            scope=tuple(
                                                _legacy(item) for item in value.scope
                                            ),
                                        )
                                    value = restore_portable_value(
                                        portable_value(value)
                                    )
                                differences.append(replace(difference, proposed=value))
                            decisions.append(
                                replace(decision, differences=tuple(differences))
                            )
                        snapshot = _snapshot(
                            frozen, replace(result, decisions=tuple(decisions))
                        )
                        parent = next(
                            row for row in snapshot.rows if row.dataset == "terms"
                        )
                        for owner in (
                            row for row in snapshot.rows if row.dataset == "contacts"
                        ):
                            intent = next(
                                item
                                for item in owner.fields
                                if item.field == "property_payment_term_id"
                            )
                            self.assertEqual(
                                intent.value,
                                LogicalReference("incoming", ("T1",), dataset="terms"),
                                shape,
                            )
                            self.assertEqual(
                                intent.dependency_row_ids, (parent.row_id,), shape
                            )
                            self.assertLess(
                                parent.schedule_ordinal, owner.schedule_ordinal
                            )

    def test_existing_update_keeps_reference_only_parent_as_a_bound_target_link(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    frozen, result = _comparison(major, mode, reference_parent=True)
                    self.assertEqual(
                        result.counts,
                        {
                            "CREATE": 0,
                            "UPDATE": 1,
                            "UNCHANGED": 0,
                            "BLOCKED": 0,
                            "AMBIGUOUS": 0,
                        },
                    )
                    snapshot = _snapshot(frozen, result)
                    self.assertEqual(
                        {row.dataset for row in snapshot.rows}, {"contacts"}
                    )
                    owner = snapshot.rows[0]
                    intent = next(
                        item
                        for item in owner.fields
                        if item.field == "property_payment_term_id"
                    )
                    self.assertIsInstance(intent.value, BusinessReference)
                    self.assertEqual(intent.value.model, "account.payment.term")
                    self.assertEqual(intent.value.key, ("Immediate",))
                    self.assertEqual(intent.value.scope[0].key, ("North",))
                    self.assertEqual(intent.value.scope[0].target_fields, ("name",))
                    self.assertEqual(intent.dependency_row_ids, ())
                    self.assertEqual(
                        owner.target_binding_hash,
                        target_record_binding_hash("res.partner", 50),
                    )

    def test_unresolved_identity_scope_remains_a_reviewable_blocked_snapshot(self):
        for matches in (0, 2):
            with self.subTest(matches=matches):
                frozen, result = _comparison(company_matches=matches)
                self.assertGreater(result.counts["BLOCKED"], 0)
                snapshot = _snapshot(frozen, result)
                self.assertEqual(snapshot.counts, result.counts)
                self.assertEqual(len(snapshot.rows), sum(result.counts.values()))
                self.assertTrue(all(row.schedule_ordinal < 0 for row in snapshot.rows))

    def test_execution_rows_add_governed_fields_without_rewriting_comparison(self):
        frozen, result = _comparison()
        parent_decision = next(
            item for item in result.decisions if item.dataset == "terms"
        )
        self.assertEqual(parent_decision.business_scope[0].target_fields, ())
        snapshot = _snapshot(frozen, result)
        parent_row = next(item for item in snapshot.rows if item.dataset == "terms")
        self.assertEqual(parent_row.business_scope[0].key, ("North",))
        self.assertEqual(parent_row.business_scope[0].target_fields, ("name",))
        self.assertEqual(parent_decision.business_scope[0].target_fields, ())
