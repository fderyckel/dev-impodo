from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from uuid import uuid4

from impodo.domain.odoo_capture import (
    OdooCaptureContractError,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
    odoo_column_stable_key,
    odoo_dataset_id,
)
from impodo.domain.source_binding import FileSourceBinding, SourceOriginKind
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
            project_id=str(uuid4()),
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
            selection.dataset_id,
            odoo_dataset_id(selection.project_id, selection.model),
        )
        self.assertEqual(len(selection.column_stable_keys), 2)
        self.assertNotIn("credential", selection.to_json().casefold())
        self.assertNotIn('"id"', selection.to_json())
        self.assertEqual(
            odoo_dataset_id(selection.project_id, selection.model),
            odoo_dataset_id(selection.project_id, selection.model),
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

    @staticmethod
    def _selection() -> OdooCaptureSelection:
        return OdooCaptureSelection.create(
            selection_id=str(uuid4()),
            version=1,
            project_id=str(uuid4()),
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
