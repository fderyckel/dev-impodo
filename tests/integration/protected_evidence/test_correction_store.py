"""Verify encrypted correction-plan and confirmation storage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from impodo.adapters.protected_correction_store import ProtectedCorrectionStore
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanField,
    CorrectionValueKind,
)
from impodo.domain.cutover.models import MigrationCutoverError
from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from tests.domain.test_correction_origin import _index, _manifest


HASHES = tuple("sha256:" + character * 64 for character in "123456789abc")
IDS = tuple(f"{value:08d}-0000-4000-8000-000000000000" for value in range(1, 7))
NOW = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
ACTOR = ActorIdentity("test", "data-manager", "Data manager")


def _plan() -> CorrectionPlan:
    return CorrectionPlan.create(
        plan_id=IDS[0],
        project_id=IDS[1],
        completed_migration_run_id=IDS[2],
        successor_migration_run_id=IDS[3],
        workspace_id=IDS[4],
        origin_evidence_hash=HASHES[4],
        previous_prepared_hash=HASHES[5],
        corrected_prepared_hash=HASHES[6],
        read_credential_binding_hash=HASHES[7],
        read_identity=OdooReadIdentity(
            target_hash=HASHES[0],
            principal_hash=HASHES[1],
            permission_hash=HASHES[2],
            context_hash=HASHES[3],
            readable_models=("product.template",),
            observed_at="2026-08-28T04:00:00Z",
        ),
        fields=(
            CorrectionPlanField(
                dataset="Products",
                source_row=1,
                row_id="row-1",
                target_model="product.template",
                odoo_id=701,
                completed_disposition="CREATE",
                target_binding_hash="",
                target_field="active",
                value_kind=CorrectionValueKind.SCALAR,
                previous=False,
                current=False,
                corrected=True,
            ),
        ),
        created_by=ACTOR,
        created_at=NOW,
    )


def _confirmation(plan: CorrectionPlan) -> CorrectionConfirmation:
    return CorrectionConfirmation.create(
        confirmation_id=IDS[5],
        plan=plan,
        write_credential_binding_hash=HASHES[11],
        write_identity=OdooWriteIdentity(
            target_hash=HASHES[0],
            principal_hash=HASHES[8],
            permission_hash=HASHES[9],
            context_hash=HASHES[10],
            readable_models=("product.template",),
            writable_models=("product.template",),
            observed_at="2026-08-28T04:01:00Z",
        ),
        confirmed_by=ACTOR,
        confirmed_at=NOW,
    )


class ProtectedCorrectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProtectedCorrectionStore(
            ProtectedProjectEvidenceStore(self.root, MemorySecretStore())
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_and_confirmation_round_trip_in_separate_protected_paths(self) -> None:
        plan = _plan()
        confirmation = _confirmation(plan)

        plan_reference = self.store.put_plan(plan)
        confirmation_reference = self.store.put_confirmation(
            plan,
            confirmation,
        )

        self.assertIn("/correction-plans/", plan_reference.storage_key)
        self.assertIn(
            "/correction-confirmations/",
            confirmation_reference.storage_key,
        )
        self.assertEqual(self.store.read_plan(plan_reference), plan)
        self.assertEqual(
            self.store.read_confirmation(confirmation_reference),
            confirmation,
        )
        encrypted = (
            self.root
            / ".project-evidence-protected"
            / plan_reference.storage_key
        ).read_bytes()
        self.assertNotIn(b'"odoo_id"', encrypted)
        self.assertNotIn(b'"corrected"', encrypted)

    def test_origin_and_target_index_round_trip_in_protected_paths(self) -> None:
        index = _index()
        manifest = _manifest(index)

        index_reference = self.store.put_target_index(index)
        manifest_reference = self.store.put_origin(manifest)

        self.assertIn(
            "/correction-target-indexes/",
            index_reference.storage_key,
        )
        self.assertIn("/correction-origins/", manifest_reference.storage_key)
        self.assertEqual(self.store.read_target_index(index_reference), index)
        self.assertEqual(self.store.read_origin(manifest_reference), manifest)

    def test_ciphertext_tampering_fails_before_plan_parsing(self) -> None:
        reference = self.store.put_plan(_plan())
        path = (
            self.root
            / ".project-evidence-protected"
            / reference.storage_key
        )
        encrypted = bytearray(path.read_bytes())
        encrypted[-1] ^= 1
        path.write_bytes(encrypted)

        with self.assertRaisesRegex(MigrationCutoverError, "hash changed"):
            self.store.read_plan(reference)


if __name__ == "__main__":
    unittest.main()
