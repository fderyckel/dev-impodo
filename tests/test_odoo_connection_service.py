from __future__ import annotations

import unittest

from impodo.application.odoo_connection_service import (
    OdooConnectionPurpose,
    OdooConnectionTestService,
)
from impodo.models import OdooReadIdentity, TargetFingerprint, target_identity_hash
from impodo.workspace_state import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStateError,
    SourceMode,
)


class OdooConnectionTestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_state = WorkspaceState(
            workspace_id="3a9127fa-8db4-47f9-9883-55f9fd4432e7",
            name="Odoo source",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="migration",
        )
        self.target_hash = target_identity_hash(
            connection_mode="REMOTE",
            base_url=self.workspace_state.odoo_base_url,
            database=self.workspace_state.odoo_database,
        )
        self.fingerprint_calls: list[tuple[str, str]] = []
        self.identity_calls: list[tuple[str, str, tuple[str, ...]]] = []

        def fingerprint_probe(workspace_state, api_key):
            self.fingerprint_calls.append((workspace_state.workspace_id, api_key))
            return TargetFingerprint(
                target_hash=self.target_hash,
                connection_mode="REMOTE",
                database="migration",
                odoo_version="19.0",
                snapshot_timestamp="2026-08-21T00:00:00Z",
            )

        def identity_probe(workspace_state, api_key, models):
            normalized = tuple(models)
            self.identity_calls.append(
                (workspace_state.workspace_id, api_key, normalized)
            )
            return OdooReadIdentity(
                target_hash=self.target_hash,
                principal_hash="sha256:" + "1" * 64,
                permission_hash="sha256:" + "2" * 64,
                context_hash="sha256:" + "3" * 64,
                readable_models=normalized,
                observed_at="2026-08-21T00:00:00Z",
            )

        self.service = OdooConnectionTestService(
            fingerprint_probe,
            identity_probe,
        )

    def test_read_check_is_bounded_to_connection_and_self_identity(self) -> None:
        result = self.service.test_read(
            self.workspace_state,
            "read-key",
            purpose=OdooConnectionPurpose.SOURCE_READ,
        )

        self.assertIs(result.purpose, OdooConnectionPurpose.SOURCE_READ)
        self.assertEqual(result.connection.identity_hash, self.target_hash)
        self.assertEqual(
            self.fingerprint_calls,
            [(self.workspace_state.workspace_id, "read-key")],
        )
        self.assertEqual(
            self.identity_calls,
            [(self.workspace_state.workspace_id, "read-key", ("res.users",))],
        )

    def test_write_access_is_not_conflated_with_read_connection_testing(self) -> None:
        with self.assertRaisesRegex(WorkspaceStateError, "load confirmation"):
            self.service.test_read(
                self.workspace_state,
                "write-key",
                purpose=OdooConnectionPurpose.TARGET_WRITE,
            )
        self.assertEqual(self.fingerprint_calls, [])
        self.assertEqual(self.identity_calls, [])


if __name__ == "__main__":
    unittest.main()

