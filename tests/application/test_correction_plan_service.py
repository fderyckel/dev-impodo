"""Verify blocker-free review publication through the correction service."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.application.correction_service import (
    CorrectionPlanService,
    CorrectionReview,
    CorrectionReviewedField,
    CorrectionTargetIndexEntry,
)
from impodo.domain.correction import (
    CorrectionCandidate,
    CorrectionFieldDecision,
    CorrectionFieldOutcome,
    CorrectionPlanError,
    CorrectionValueKind,
)
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity


HASHES = tuple("sha256:" + character * 64 for character in "123456789abc")
IDS = tuple(f"{value:08d}-0000-4000-8000-000000000000" for value in range(1, 7))
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = ActorIdentity("test", "data-manager", "Data manager")


def _reviewed(
    source_row: int,
    *,
    field: str,
    previous,
    current,
    corrected,
    outcome: CorrectionFieldOutcome,
) -> CorrectionReviewedField:
    candidate = CorrectionCandidate(
        dataset="Products",
        source_row=source_row,
        target_model="product.template",
        target_field=field,
        value_kind=CorrectionValueKind.SCALAR,
        previous=previous,
        corrected=corrected,
    )
    return CorrectionReviewedField(
        target=CorrectionTargetIndexEntry(
            dataset="Products",
            source_row=source_row,
            row_id=f"row-{source_row}",
            target_model="product.template",
            odoo_id=700 + source_row,
            completed_disposition="CREATE",
            target_binding_hash="",
        ),
        decision=CorrectionFieldDecision(candidate, current, outcome),
    )


def _read_identity() -> OdooReadIdentity:
    return OdooReadIdentity(
        target_hash=HASHES[0],
        principal_hash=HASHES[1],
        permission_hash=HASHES[2],
        context_hash=HASHES[3],
        readable_models=("product.template",),
        observed_at="2026-08-28T04:00:00Z",
    )


class CorrectionPlanServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CorrectionPlanService()

    def create_plan(self, review: CorrectionReview):
        return self.service.create_plan(
            review,
            plan_id=IDS[0],
            project_id=IDS[1],
            completed_migration_run_id=IDS[2],
            successor_migration_run_id=IDS[3],
            workspace_id=IDS[4],
            origin_evidence_hash=HASHES[4],
            previous_prepared_hash=HASHES[5],
            corrected_prepared_hash=HASHES[6],
            read_credential_binding_hash=HASHES[7],
            read_identity=_read_identity(),
            created_by=ACTOR,
            created_at=NOW,
        )

    def test_scalar_plan_covers_rule_output_not_editor_type(self) -> None:
        review = CorrectionReview(
            target_hash=HASHES[0],
            fields=(
                _reviewed(
                    2,
                    field="name",
                    previous="widget",
                    current="widget",
                    corrected="WIDGET",
                    outcome=CorrectionFieldOutcome.READY,
                ),
                _reviewed(
                    1,
                    field="active",
                    previous=False,
                    current=False,
                    corrected=True,
                    outcome=CorrectionFieldOutcome.READY,
                ),
            ),
            blockers=(),
        )

        plan = self.create_plan(review)

        self.assertEqual(
            tuple(item.target_field for item in plan.fields),
            ("active", "name"),
        )
        self.assertEqual(plan.public_summary().field_count, 2)

    def test_unreported_conflict_still_blocks_the_whole_plan(self) -> None:
        review = CorrectionReview(
            target_hash=HASHES[0],
            fields=(
                _reviewed(
                    1,
                    field="active",
                    previous=False,
                    current=False,
                    corrected=True,
                    outcome=CorrectionFieldOutcome.READY,
                ),
                _reviewed(
                    2,
                    field="name",
                    previous="widget",
                    current="independent edit",
                    corrected="WIDGET",
                    outcome=CorrectionFieldOutcome.CONFLICT,
                ),
            ),
            blockers=(),
        )

        with self.assertRaisesRegex(CorrectionPlanError, "not eligible"):
            self.create_plan(review)

    def test_confirmation_binds_a_separate_write_principal(self) -> None:
        plan = self.create_plan(
            CorrectionReview(
                target_hash=HASHES[0],
                fields=(
                    _reviewed(
                        1,
                        field="active",
                        previous=False,
                        current=False,
                        corrected=True,
                        outcome=CorrectionFieldOutcome.READY,
                    ),
                ),
                blockers=(),
            )
        )
        write_identity = OdooWriteIdentity(
            target_hash=HASHES[0],
            principal_hash=HASHES[8],
            permission_hash=HASHES[9],
            context_hash=HASHES[10],
            readable_models=("product.template",),
            writable_models=("product.template",),
            observed_at="2026-08-28T04:01:00Z",
        )

        confirmation = self.service.confirm(
            plan,
            confirmation_id=IDS[5],
            write_credential_binding_hash=HASHES[11],
            write_identity=write_identity,
            confirmed_by=ACTOR,
            confirmed_at=NOW,
        )

        self.assertEqual(confirmation.plan_hash, plan.plan_hash)
        self.assertEqual(confirmation.write_principal_hash, HASHES[8])
        self.assertNotEqual(
            confirmation.write_principal_hash,
            plan.read_principal_hash,
        )


if __name__ == "__main__":
    unittest.main()
