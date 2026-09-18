from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from impodo.adapters.protected_odoo_capture_filters import ProtectedOdooCaptureFilterStore
from impodo.adapters.protected_project_evidence_store import ProtectedProjectEvidenceStore
from impodo.domain.odoo_capture import (
    OdooCaptureFilterClause,
    OdooCaptureFilterOperator,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from impodo.domain.workspace.errors import WorkspaceError


PROJECT = "00000000-0000-0000-0000-000000000098"
DATA_VERSION = "00000000-0000-0000-0000-000000000097"
SELECTION = "00000000-0000-0000-0000-000000000096"
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


class ProtectedOdooCaptureFilterTests(unittest.TestCase):
    def test_encrypts_root_value_and_fails_closed_on_changed_artifact(self) -> None:
        with TemporaryDirectory() as root:
            evidence = ProtectedProjectEvidenceStore(root, _Secrets())
            filters = ProtectedOdooCaptureFilterStore(evidence)
            clause = OdooCaptureFilterClause(
                "ref", OdooCaptureFilterOperator.EQUALS, ("FICTIONAL-ROOT-91",)
            )
            artifact_hash = filters.put(
                PROJECT,
                selection_id=SELECTION,
                version=1,
                data_version_id=DATA_VERSION,
                clauses=(clause,),
            )
            selection = OdooCaptureSelection.create(
                selection_id=SELECTION,
                version=1,
                data_version_id=DATA_VERSION,
                dataset_name="contacts",
                model="res.partner",
                field_names=("ref",),
                protected_filter_artifact_hash=artifact_hash,
                filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
                max_rows=10_000,
                connection_target_hash=HASH,
                schema_scope_hash=HASH,
                read_principal_hash=HASH,
                read_permission_hash=HASH,
                context_hash=HASH,
                created_at=datetime.now(timezone.utc),
                created_by="Manager",
            )
            self.assertEqual(filters.read(PROJECT, selection), (clause,))
            self.assertNotIn("FICTIONAL-ROOT-91", selection.to_json())
            encrypted_files = tuple(Path(root).rglob("*.ipe"))
            self.assertEqual(len(encrypted_files), 1)
            self.assertNotIn(b"FICTIONAL-ROOT-91", encrypted_files[0].read_bytes())
            with self.assertRaisesRegex(WorkspaceError, "unavailable or changed"):
                filters.read(PROJECT, OdooCaptureSelection.create(
                    selection_id=SELECTION,
                    version=2,
                    data_version_id=DATA_VERSION,
                    dataset_name="contacts",
                    model="res.partner",
                    field_names=("ref",),
                    protected_filter_artifact_hash=artifact_hash,
                    filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
                    max_rows=10_000,
                    connection_target_hash=HASH,
                    schema_scope_hash=HASH,
                    read_principal_hash=HASH,
                    read_permission_hash=HASH,
                    context_hash=HASH,
                    created_at=datetime.now(timezone.utc),
                    created_by="Manager",
                ))
            encrypted_files[0].write_bytes(encrypted_files[0].read_bytes() + b"changed")
            with self.assertRaisesRegex(WorkspaceError, "unavailable or changed"):
                filters.read(PROJECT, selection)


if __name__ == "__main__":
    unittest.main()
