from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.odoo_capture import (
    OdooCaptureContractError,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
    odoo_column_stable_key,
    odoo_dataset_id,
)
from impodo.domain.odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    ODOO_SOURCE_POLICY_HASH,
    ProductionWriteDisposition,
    ProtectedEvidenceEncryption,
    TargetInstanceAssurance,
)
from impodo.domain.source_binding import FileSourceBinding, SourceOriginKind
from impodo.domain.serialization import content_hash
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


HASH = "sha256:" + "1" * 64


class OdooCaptureContractTests(unittest.TestCase):
    def test_file_binding_uses_the_current_discriminated_contract(self) -> None:
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            created_at=datetime.now(timezone.utc),
            created_by="Data Manager",
            datasets=(
                SourceDataset(
                    dataset_id=str(uuid4()),
                    name="customers",
                    source=FileSourceBinding(
                        file_id=str(uuid4()),
                        table_key="csv",
                        source_sha256="a" * 64,
                        catalog_hash=HASH,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=1,
                    columns=(
                        SourceDatasetColumn(
                            ordinal=1,
                            source_name="Name",
                            stable_key="column:name",
                            candidate_type="string",
                        ),
                    ),
                ),
            ),
            content_hash=HASH,
        )
        binding = selection.datasets[0].source

        self.assertEqual(selection.origins, frozenset({SourceOriginKind.FILE}))
        self.assertEqual(binding.origin, SourceOriginKind.FILE)
        self.assertEqual(binding.source_sha256, "sha256:" + "a" * 64)
        payload = json.loads(selection.to_json())
        self.assertEqual(payload["datasets"][0]["source"]["origin"], "FILE")
        self.assertEqual(SourceSelection.from_json(selection.to_json()), selection)

        payload["datasets"][0]["file_id"] = "old-flat-file-field"
        with self.assertRaisesRegex(ValueError, "current contract"):
            SourceSelection.from_json(json.dumps(payload))

    def test_odoo_selection_is_deterministic_bounded_and_credential_free(self) -> None:
        selection = self._selection()

        restored = OdooCaptureSelection.from_json(selection.to_json())
        binding = restored.source_binding

        self.assertEqual(restored, selection)
        self.assertEqual(binding.origin, SourceOriginKind.ODOO)
        self.assertEqual(binding.capture_selection_hash, selection.content_hash)
        self.assertEqual(
            selection.policy_hash,
            ODOO_SOURCE_POLICY_HASH,
        )
        self.assertEqual(binding.policy_hash, selection.policy_hash)
        self.assertEqual(binding.source_evidence_hash, selection.content_hash)
        self.assertEqual(
            selection.dataset_id,
            odoo_dataset_id(selection.data_version_id, selection.model),
        )
        self.assertEqual(len(selection.column_stable_keys), 2)
        self.assertNotIn("credential", selection.to_json().casefold())
        self.assertNotIn('"id"', selection.to_json())
        self.assertEqual(
            odoo_dataset_id(selection.data_version_id, selection.model),
            odoo_dataset_id(selection.data_version_id, selection.model),
        )
        self.assertNotEqual(
            odoo_column_stable_key(selection.model, "name"),
            odoo_column_stable_key(selection.model, "active"),
        )

    def test_odoo_selection_rejects_widening_and_tampering(self) -> None:
        selection = self._selection()

        with self.assertRaises(OdooCaptureContractError):
            replace(selection, field_names=("id", "name"))
        with self.assertRaises(OdooCaptureContractError):
            replace(selection, max_rows=10_001)
        with self.assertRaises(OdooCaptureContractError):
            replace(selection, content_hash="sha256:" + "f" * 64)
        payload = json.loads(selection.to_json())
        payload["retired_field"] = "ignored-by-old-builds"
        with self.assertRaisesRegex(OdooCaptureContractError, "current contract"):
            OdooCaptureSelection.from_json(json.dumps(payload))
        with self.assertRaisesRegex(OdooCaptureContractError, "current source policy"):
            replace(selection, policy_hash="sha256:" + "9" * 64)

    def test_current_policy_fails_closed_for_production_writes(self) -> None:
        policy = CURRENT_ODOO_SOURCE_POLICY

        self.assertEqual(policy.odoo_major_version, 19)
        self.assertEqual(policy.api, "JSON-2")
        self.assertEqual(
            policy.target_instance_assurance,
            TargetInstanceAssurance.CONNECTION_ONLY,
        )
        self.assertEqual(
            policy.production_write_disposition,
            ProductionWriteDisposition.PRODUCTION_WRITE_UNSUPPORTED,
        )
        self.assertEqual(
            policy.protected_evidence_encryption,
            ProtectedEvidenceEncryption.APPLICATION_LEVEL_REQUIRED,
        )
        self.assertGreater(policy.max_project_history_bytes, policy.max_snapshot_bytes)

    def test_new_selection_hashes_its_manifest_once(self) -> None:
        with patch(
            "impodo.domain.odoo_capture.content_hash",
            wraps=content_hash,
        ) as hash_manifest:
            selection = self._selection()

        self.assertRegex(selection.content_hash, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(hash_manifest.call_count, 1)

    @staticmethod
    def _selection() -> OdooCaptureSelection:
        return OdooCaptureSelection.create(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            dataset_name="odoo_contacts",
            model="res.partner",
            field_names=("active", "name"),
            filter_policy=OdooCaptureFilterPolicy.ACTIVE_RECORDS,
            max_rows=1_000,
            connection_target_hash=HASH,
            schema_scope_hash="sha256:" + "2" * 64,
            read_principal_hash="sha256:" + "3" * 64,
            read_permission_hash="sha256:" + "4" * 64,
            context_hash="sha256:" + "5" * 64,
            created_at=datetime.now(timezone.utc),
            created_by="Data Manager",
        )


if __name__ == "__main__":
    unittest.main()
