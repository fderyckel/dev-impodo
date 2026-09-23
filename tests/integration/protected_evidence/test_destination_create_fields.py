from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from impodo.adapters.protected_destination_create_fields import (
    ProtectedDestinationCreateFieldStore,
)
from impodo.adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.workspace.destination_matching import (
    DestinationCreateFieldDecision,
    DestinationCreateFieldEvidence,
    DestinationCreateFieldEvidenceValue,
    DestinationMatchPlan,
)
from impodo.domain.workspace.errors import WorkspaceError


PROJECT = "00000000-0000-0000-0000-000000000091"
WORKSPACE = "00000000-0000-0000-0000-000000000092"
EVIDENCE = "00000000-0000-0000-0000-000000000093"
HASH = "sha256:" + "a" * 64


class _Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, persistent: bool) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


class ProtectedDestinationCreateFieldStoreTests(unittest.TestCase):
    def test_value_is_encrypted_and_bound_to_the_portable_plan(self) -> None:
        now = datetime.now(UTC)
        value = DestinationCreateFieldEvidenceValue(
            dataset_id="contacts",
            model="res.partner",
            field_name="group_on",
            field_type="selection",
            value="sale_order",
            display_value="Sales order (sale_order)",
            field_contract_hash=HASH,
            value_hash=HASH,
        )
        evidence = DestinationCreateFieldEvidence(
            evidence_id=EVIDENCE,
            workspace_id=WORKSPACE,
            source_selection_hash=HASH,
            source_schema_hash=HASH,
            destination_target_hash=HASH,
            destination_read_principal_hash=HASH,
            destination_read_context_hash=HASH,
            destination_schema_snapshot_hash=HASH,
            values=(value,),
            recorded_at=now,
        )
        plan = DestinationMatchPlan(
            workspace_id=WORKSPACE,
            source_selection_hash=HASH,
            source_schema_hash=HASH,
            destination_target_hash=HASH,
            destination_credential_binding_hash=HASH,
            destination_read_principal_hash=HASH,
            destination_read_permission_hash=HASH,
            destination_read_context_hash=HASH,
            destination_schema_snapshot_hash=HASH,
            destination_record_snapshot_hash=HASH,
            model_matches=(),
            recorded_at=now,
            recorded_by="Manager",
            create_field_decisions=(
                DestinationCreateFieldDecision(
                    dataset_id="contacts",
                    model="res.partner",
                    model_label="Contacts",
                    field_name="group_on",
                    field_label="Group on",
                    field_type="selection",
                    provider_kind="odoo_default",
                    decision_kind="review",
                    reason="This default selects an Odoo workflow choice.",
                    field_contract_hash=HASH,
                    value_hash=HASH,
                ),
            ),
            create_field_evidence_id=EVIDENCE,
            create_field_evidence_hash=evidence.content_hash,
            create_field_evidence=evidence,
        )

        with TemporaryDirectory() as root:
            store = ProtectedDestinationCreateFieldStore(
                ProtectedProjectEvidenceStore(root, _Secrets())
            )
            stored = store.put(PROJECT, plan)
            self.assertIsNone(stored.create_field_evidence)
            self.assertNotIn("sale_order", stored.to_json())
            self.assertEqual(store.read(PROJECT, stored), evidence)
            encrypted = tuple(Path(root).rglob("*.ipe"))
            self.assertEqual(len(encrypted), 1)
            self.assertNotIn(b"sale_order", encrypted[0].read_bytes())
            encrypted[0].write_bytes(encrypted[0].read_bytes() + b"changed")
            with self.assertRaisesRegex(WorkspaceError, "unavailable or changed"):
                store.read(PROJECT, stored)


if __name__ == "__main__":
    unittest.main()
