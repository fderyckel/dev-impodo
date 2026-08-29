"""Verify whole-plan hashing and explicit correction confirmation."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import unittest

from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanError,
    CorrectionPlanField,
    CorrectionValueKind,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity


HASHES = tuple("sha256:" + character * 64 for character in "123456789abc")
PLAN_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
COMPLETED_RUN_ID = "33333333-3333-4333-8333-333333333333"
SUCCESSOR_RUN_ID = "44444444-4444-4444-8444-444444444444"
WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
CONFIRMATION_ID = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = ActorIdentity("test", "data-manager", "Data manager")


def _field(source_row: int, field: str, previous, corrected) -> CorrectionPlanField:
    return CorrectionPlanField(
        dataset="Products",
        source_row=source_row,
        row_id=f"row-{source_row}",
        target_model="product.template",
        odoo_id=700 + source_row,
        completed_disposition="CREATE",
        target_binding_hash="",
        target_field=field,
        value_kind=CorrectionValueKind.SCALAR,
        previous=previous,
        current=previous,
        corrected=corrected,
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


def _write_identity(
    *,
    principal_hash: str = HASHES[8],
    observed_at: str = "2026-08-28T04:01:00Z",
) -> OdooWriteIdentity:
    return OdooWriteIdentity(
        target_hash=HASHES[0],
        principal_hash=principal_hash,
        permission_hash=HASHES[9],
        context_hash=HASHES[10],
        readable_models=("product.template",),
        writable_models=("product.template",),
        observed_at=observed_at,
    )


def make_plan(
    fields: tuple[CorrectionPlanField, ...] | None = None,
) -> CorrectionPlan:
    return CorrectionPlan.create(
        plan_id=PLAN_ID,
        project_id=PROJECT_ID,
        completed_migration_run_id=COMPLETED_RUN_ID,
        successor_migration_run_id=SUCCESSOR_RUN_ID,
        workspace_id=WORKSPACE_ID,
        origin_evidence_hash=HASHES[4],
        previous_prepared_hash=HASHES[5],
        corrected_prepared_hash=HASHES[6],
        read_credential_binding_hash=HASHES[7],
        read_identity=_read_identity(),
        fields=fields
        or (
            _field(2, "name", "widget", "WIDGET"),
            _field(1, "active", False, True),
        ),
        created_by=ACTOR,
        created_at=NOW,
    )


class CorrectionPlanTests(unittest.TestCase):
    def test_plan_hash_is_stable_across_review_page_order(self) -> None:
        fields = (
            _field(2, "name", "widget", "WIDGET"),
            _field(1, "active", False, True),
        )

        first = make_plan(fields)
        second = make_plan(tuple(reversed(fields)))

        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertEqual(first.fields, second.fields)

    def test_public_summary_contains_no_ids_values_or_hashes(self) -> None:
        plan = make_plan()

        rendered = canonical_json(asdict(plan.public_summary()))

        self.assertNotIn("odoo_id", rendered)
        self.assertNotIn("widget", rendered.casefold())
        self.assertNotIn("sha256:", rendered)
        self.assertEqual(plan.public_summary().field_count, 2)
        self.assertEqual(plan.public_summary().record_count, 2)

    def test_protected_round_trip_detects_changed_field_value(self) -> None:
        plan = make_plan()
        payload = json.loads(plan.protected_json())
        payload["fields"][0]["corrected"] = False

        with self.assertRaisesRegex(CorrectionPlanError, "hash changed"):
            CorrectionPlan.from_protected_json(
                canonical_json(payload).encode("utf-8")
            )

    def test_relationship_field_requires_exact_protected_identities(self) -> None:
        field = replace(
            _field(1, "uom_id", 41, 42),
            value_kind=CorrectionValueKind.MANY2ONE,
            current=41,
        )

        self.assertEqual(make_plan((field,)).fields[0].corrected, 42)
        with self.assertRaisesRegex(CorrectionPlanError, "relationship identity"):
            replace(field, corrected=("Unit",))

    def test_same_exact_target_field_cannot_be_planned_twice(self) -> None:
        first = _field(1, "active", False, True)
        duplicate = replace(
            _field(2, "active", False, True),
            odoo_id=first.odoo_id,
        )

        with self.assertRaisesRegex(CorrectionPlanError, "ambiguous exact"):
            make_plan((first, duplicate))

    def test_confirmation_rejects_a_changed_write_principal(self) -> None:
        plan = make_plan()
        confirmation = CorrectionConfirmation.create(
            confirmation_id=CONFIRMATION_ID,
            plan=plan,
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(),
            confirmed_by=ACTOR,
            confirmed_at=NOW,
        )

        confirmation.assert_current(
            plan,
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(observed_at="2026-08-28T04:02:00Z"),
        )
        with self.assertRaisesRegex(CorrectionPlanError, "stale"):
            confirmation.assert_current(
                plan,
                write_credential_binding_hash=HASHES[11],
                write_identity=_write_identity(principal_hash=HASHES[1]),
            )
        with self.assertRaisesRegex(CorrectionPlanError, "stale"):
            confirmation.assert_current(
                plan,
                write_credential_binding_hash=HASHES[11],
                write_identity=_write_identity(
                    observed_at="2026-08-28T04:00:00Z"
                ),
            )

    def test_confirmation_round_trip_verifies_its_whole_hash(self) -> None:
        confirmation = CorrectionConfirmation.create(
            confirmation_id=CONFIRMATION_ID,
            plan=make_plan(),
            write_credential_binding_hash=HASHES[11],
            write_identity=_write_identity(),
            confirmed_by=ACTOR,
            confirmed_at=NOW,
        )

        restored = CorrectionConfirmation.from_protected_json(
            confirmation.protected_json()
        )

        self.assertEqual(restored, confirmation)


if __name__ == "__main__":
    unittest.main()
