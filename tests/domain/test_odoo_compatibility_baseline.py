"""Preserve Odoo 19 policy and evidence identities during dual-support work."""

from dataclasses import replace
import hashlib
import json
import unittest

from impodo.domain.odoo.contracts import (
    record_snapshot_from_json,
    record_snapshot_json,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASH
from tests.support.paths import REPOSITORY_ROOT


FIXTURES = REPOSITORY_ROOT / "fixtures" / "odoo-compatibility"
BASELINE = json.loads((FIXTURES / "odoo19-baseline.json").read_text(encoding="utf-8"))


class Odoo19CompatibilityBaselineTests(unittest.TestCase):
    def test_existing_odoo19_policy_hashes_remain_stable(self):
        self.assertEqual(ODOO_SOURCE_POLICY_HASH, BASELINE["source_policy_hash"])
        self.assertEqual(REFERENCE_POLICY_HASH, BASELINE["reference_policy_hash"])

    def test_connection_identity_keeps_existing_canonical_hash(self):
        self.assertEqual(
            target_identity_hash(
                connection_mode=" remote ",
                base_url="https://odoo.example.test/",
                database=" impodo_baseline ",
            ),
            BASELINE["connection_hash"],
        )

    def test_stored_record_evidence_restores_and_serializes_without_change(self):
        original = (FIXTURES / "odoo19-record-snapshot.json").read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(), BASELINE["record_snapshot_file_sha256"])
        restored = record_snapshot_from_json(original.decode("utf-8"))
        self.assertEqual(restored.content_hash, BASELINE["record_snapshot_hash"])
        self.assertEqual((record_snapshot_json(restored) + "\n").encode(), original)
        self.assertEqual(restored.fingerprint.odoo_version, "19.0")
        self.assertIs(restored.records["res.partner"][0].values["active"], False)

    def test_version_changes_evidence_without_changing_connection_identity(self):
        original = record_snapshot_from_json(
            (FIXTURES / "odoo19-record-snapshot.json").read_text(encoding="utf-8")
        )
        upgraded = replace(original, fingerprint=replace(original.fingerprint, odoo_version="20.0"))
        restored = record_snapshot_from_json(record_snapshot_json(upgraded))
        self.assertEqual(restored.fingerprint.target_hash, original.fingerprint.target_hash)
        self.assertNotEqual(restored.content_hash, original.content_hash)


if __name__ == "__main__":
    unittest.main()
