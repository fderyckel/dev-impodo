"""Verify lean correction execution snapshots and exact-record grouping."""

from __future__ import annotations

import unittest
from dataclasses import replace

from impodo.domain.correction import CorrectionConfirmation
from impodo.domain.correction_execution import CorrectionExecutionSnapshot
from impodo.domain.shared.models import target_record_binding_hash
from tests.domain.test_correction_plan import (
    CONFIRMATION_ID,
    HASHES,
    NOW,
    _field,
    _write_identity,
    make_plan,
)


class CorrectionExecutionSnapshotTests(unittest.TestCase):
    def test_snapshot_groups_sparse_fields_and_hashes_only_plan_references(self) -> None:
        first = _field(1, "active", False, True)
        description = replace(
            _field(2, "description", "wrong", "correct"),
            dataset=first.dataset,
            source_row=first.source_row,
            row_id=first.row_id,
            target_model=first.target_model,
            odoo_id=first.odoo_id,
            target_binding_hash=first.target_binding_hash,
        )
        plan = make_plan((first, description))
        confirmation = CorrectionConfirmation.create(
            confirmation_id=CONFIRMATION_ID,
            plan=plan,
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(),
            confirmed_by=plan.created_by,
            confirmed_at=NOW,
        )

        snapshot = CorrectionExecutionSnapshot.create(
            plan,
            confirmation,
            target_database="impodo-test",
        )

        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.field_count, 2)
        self.assertEqual(
            tuple(item.target_field for item in snapshot.records[0].fields),
            ("active", "description"),
        )
        reference_keys = set(snapshot._reference_dict())
        self.assertNotIn("records", reference_keys)
        self.assertNotIn("values", reference_keys)
        self.assertTrue(snapshot.semantic_hash.startswith("sha256:"))

    def test_changed_exact_target_binding_is_rejected(self) -> None:
        original = _field(1, "active", False, True)
        field = replace(
            original,
            odoo_id=999,
            target_binding_hash=target_record_binding_hash(
                original.target_model,
                original.odoo_id,
            ),
        )

        with self.assertRaisesRegex(Exception, "target binding changed"):
            plan = make_plan((field,))
            confirmation = CorrectionConfirmation.create(
                confirmation_id=CONFIRMATION_ID,
                plan=plan,
                write_credential_binding_hash=HASHES[11],
                write_identity=_write_identity(),
                confirmed_by=plan.created_by,
                confirmed_at=NOW,
            )
            CorrectionExecutionSnapshot.create(
                plan,
                confirmation,
                target_database="impodo-test",
            )


if __name__ == "__main__":
    unittest.main()
