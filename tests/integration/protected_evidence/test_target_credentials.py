from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from keyring.errors import KeyringError

from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode
from impodo.adapters.protected_evidence.credential_vault import CredentialVault, MemorySecretStore, PROTECTED_EVIDENCE_SERVICE_NAME, READ_SERVICE_NAME, WRITE_SERVICE_NAME
from impodo.application.shared.secrets import DESTINATION_READ_SERVICE_NAME
from impodo.application.shared.secrets import SecretStoreError
from impodo.web.target_credentials import (
    TargetCredentialRole,
    TargetCredentialRemovalReason,
    delete_target_credential,
    delete_target_credentials,
    destination_read_credential_id,
    destination_transfer_credential_id,
    get_target_credential,
    get_target_credential_status,
    local_read_credential_binding_hash,
    store_target_credential,
    target_read_credential_id,
    target_write_credential_id,
)


class TargetCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.workspace_state = WorkspaceState(
            workspace_id="project-1",
            name="Migration",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://example.odoo.com",
            odoo_database="production",
        )

    def test_read_and_write_credentials_are_separate_target_bound_envelopes(
        self,
    ) -> None:
        read = store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )

        self.assertNotEqual(
            target_read_credential_id(self.workspace_state),
            target_write_credential_id(self.workspace_state),
        )
        self.assertEqual(
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.READ,
            ),
            read,
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.WRITE,
            )
        )
        payload = json.loads(
            self.store.values[target_read_credential_id(self.workspace_state)]
        )
        self.assertEqual(payload["role"], "READ")
        self.assertEqual(payload["secret"], "read-secret")
        self.assertRegex(read.binding_hash, r"^sha256:[0-9a-f]{64}$")

    def test_write_credential_never_falls_back_to_read_secret(
        self,
    ) -> None:
        store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.WRITE,
            )
        )

    def test_rotation_changes_only_that_roles_safe_binding(self) -> None:
        first = store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "same-secret",
            persistent=False,
        )
        second = store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "same-secret",
            persistent=False,
        )

        self.assertNotEqual(first.binding_hash, second.binding_hash)
        self.assertNotIn("same-secret", first.binding_hash)
        self.assertFalse(first.replaced)
        self.assertTrue(second.replaced)

    def test_safe_status_distinguishes_session_and_persistent_storage(self) -> None:
        self.assertEqual(
            get_target_credential_status(
                self.store,
                self.workspace_state,
                TargetCredentialRole.READ,
            ).availability.value,
            "MISSING",
        )
        store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "session-secret",
            persistent=False,
        )
        session = get_target_credential_status(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
        )
        self.assertEqual(session.availability.value, "SESSION")
        self.assertNotIn("session-secret", repr(session))

        store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "persistent-secret",
            persistent=True,
        )
        persistent = get_target_credential_status(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
        )
        self.assertEqual(persistent.availability.value, "PERSISTENT")
        self.assertNotIn("persistent-secret", repr(persistent))

    def test_target_change_does_not_reuse_a_stored_role(self) -> None:
        store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        changed = replace(self.workspace_state, odoo_database="staging")

        self.assertNotEqual(
            target_read_credential_id(self.workspace_state),
            target_read_credential_id(changed),
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                changed,
                TargetCredentialRole.READ,
            )
        )

    def test_destination_read_credential_is_separate_from_source_read(self) -> None:
        destination = replace(
            self.workspace_state,
            destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
            destination_odoo_base_url="https://destination.example.com",
            destination_odoo_database="destination",
        )
        source = store_target_credential(
            self.store,
            destination,
            TargetCredentialRole.READ,
            "source-secret",
            persistent=False,
        )
        target = store_target_credential(
            self.store,
            destination,
            TargetCredentialRole.DESTINATION_READ,
            "destination-secret",
            persistent=False,
        )

        self.assertNotEqual(source.binding_hash, target.binding_hash)
        self.assertEqual(
            get_target_credential(
                self.store,
                destination,
                TargetCredentialRole.READ,
            ).secret,
            "source-secret",
        )
        self.assertEqual(
            get_target_credential(
                self.store,
                destination,
                TargetCredentialRole.DESTINATION_READ,
            ).secret,
            "destination-secret",
        )

    def test_invalid_role_envelope_is_rejected(self) -> None:
        write = store_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.WRITE,
            "write-secret",
            persistent=False,
        )
        self.assertRegex(write.binding_hash, r"^sha256:[0-9a-f]{64}$")
        credential_id = target_write_credential_id(self.workspace_state)
        payload = json.loads(self.store.values[credential_id])
        payload["role"] = "READ"
        self.store.values[credential_id] = json.dumps(payload)

        with self.assertRaisesRegex(SecretStoreError, "does not match"):
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.WRITE,
            )

    def test_delete_removes_both_current_roles(self) -> None:
        for role, secret in (
            (TargetCredentialRole.READ, "read-secret"),
            (TargetCredentialRole.WRITE, "write-secret"),
        ):
            store_target_credential(
                self.store,
                self.workspace_state,
                role,
                secret,
                persistent=False,
            )
        receipts = delete_target_credentials(
            self.store,
            self.workspace_state,
            reason=TargetCredentialRemovalReason.RECIPE_DELETED,
        )

        self.assertEqual(self.store.values, {})
        self.assertEqual(
            {receipt.role for receipt in receipts},
            {TargetCredentialRole.READ, TargetCredentialRole.WRITE},
        )
        self.assertTrue(
            all(receipt.credential_binding_hash for receipt in receipts)
        )
        self.assertTrue(
            all(
                receipt.reason
                is TargetCredentialRemovalReason.RECIPE_DELETED
                for receipt in receipts
            )
        )

    def test_user_can_forget_read_key_without_deleting_write_key(self) -> None:
        for role in TargetCredentialRole:
            store_target_credential(
                self.store,
                self.workspace_state,
                role,
                f"{role.value.lower()}-secret",
                persistent=False,
            )

        receipt = delete_target_credential(
            self.store,
            self.workspace_state,
            TargetCredentialRole.READ,
            reason=TargetCredentialRemovalReason.USER_REQUESTED,
        )

        self.assertIsNotNone(receipt)
        self.assertIsNone(
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.READ,
            )
        )
        self.assertIsNotNone(
            get_target_credential(
                self.store,
                self.workspace_state,
                TargetCredentialRole.WRITE,
            )
        )

    def test_local_no_key_binding_is_stable_and_target_bound(self) -> None:
        local = replace(
            self.workspace_state,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
        )
        first = local_read_credential_binding_hash(local)

        self.assertEqual(first, local_read_credential_binding_hash(local))
        self.assertNotEqual(
            first,
            local_read_credential_binding_hash(
                replace(local, odoo_database="other")
            ),
        )

    def test_operating_system_vault_uses_separate_role_service_labels(self) -> None:
        vault = CredentialVault()
        read_id = target_read_credential_id(self.workspace_state)
        write_id = target_write_credential_id(self.workspace_state)
        destination_workspace = replace(
            self.workspace_state,
            destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
            destination_odoo_base_url="https://destination.example.com",
            destination_odoo_database="destination",
        )
        destination_id = destination_read_credential_id(destination_workspace)
        self.assertEqual(
            destination_id,
            destination_transfer_credential_id(destination_workspace),
        )
        protected_id = f"{self.workspace_state.workspace_id}:protected:origin-v1"

        with patch("impodo.adapters.protected_evidence.credential_vault.keyring") as keyring:
            vault.set(read_id, "read-secret", persistent=True)
            vault.set(write_id, "write-secret", persistent=True)
            vault.set(destination_id, "destination-secret", persistent=True)
            vault.set(protected_id, "protected-key", persistent=True)
            fresh_vault = CredentialVault()
            fresh_vault.get(read_id)
            fresh_vault.get(write_id)
            fresh_vault.get(destination_id)
            fresh_vault.get(protected_id)

        keyring.set_password.assert_any_call(
            READ_SERVICE_NAME,
            read_id,
            "read-secret",
        )
        keyring.set_password.assert_any_call(
            WRITE_SERVICE_NAME,
            write_id,
            "write-secret",
        )
        keyring.set_password.assert_any_call(
            DESTINATION_READ_SERVICE_NAME,
            destination_id,
            "destination-secret",
        )
        keyring.get_password.assert_any_call(READ_SERVICE_NAME, read_id)
        keyring.get_password.assert_any_call(WRITE_SERVICE_NAME, write_id)
        keyring.get_password.assert_any_call(
            DESTINATION_READ_SERVICE_NAME,
            destination_id,
        )
        keyring.set_password.assert_any_call(
            PROTECTED_EVIDENCE_SERVICE_NAME,
            protected_id,
            "protected-key",
        )
        keyring.get_password.assert_any_call(
            PROTECTED_EVIDENCE_SERVICE_NAME,
            protected_id,
        )

    def test_failed_persistent_write_does_not_leave_a_session_secret(self) -> None:
        vault = CredentialVault()
        read_id = target_read_credential_id(self.workspace_state)

        with patch("impodo.adapters.protected_evidence.credential_vault.keyring") as keyring:
            keyring.set_password.side_effect = KeyringError("unavailable")
            keyring.get_password.return_value = None
            with self.assertRaisesRegex(SecretStoreError, "Could not save"):
                vault.set(read_id, "must-not-remain", persistent=True)

            self.assertIsNone(vault.get(read_id))

    def test_session_key_is_missing_from_a_fresh_vault_process(self) -> None:
        with patch("impodo.adapters.protected_evidence.credential_vault.keyring") as keyring:
            keyring.get_password.return_value = None
            current = CredentialVault()
            store_target_credential(
                current,
                self.workspace_state,
                TargetCredentialRole.READ,
                "session-only",
                persistent=False,
            )

            fresh = CredentialVault()
            status = get_target_credential_status(
                fresh,
                self.workspace_state,
                TargetCredentialRole.READ,
            )

        self.assertEqual(status.availability.value, "MISSING")

    def test_persistent_key_is_available_to_a_fresh_vault_process(self) -> None:
        stored: dict[tuple[str, str], str] = {}

        def set_password(service, credential_id, secret):
            stored[(service, credential_id)] = secret

        def get_password(service, credential_id):
            return stored.get((service, credential_id))

        with patch("impodo.adapters.protected_evidence.credential_vault.keyring") as keyring:
            keyring.set_password.side_effect = set_password
            keyring.get_password.side_effect = get_password
            current = CredentialVault()
            store_target_credential(
                current,
                self.workspace_state,
                TargetCredentialRole.READ,
                "persistent-read-key",
                persistent=True,
            )

            fresh = CredentialVault()
            credential = get_target_credential(
                fresh,
                self.workspace_state,
                TargetCredentialRole.READ,
            )

        self.assertIsNotNone(credential)
        self.assertEqual(credential.secret, "persistent-read-key")
        self.assertTrue(credential.persistent)

    def test_replacing_persistent_key_with_session_removes_saved_copy(self) -> None:
        stored: dict[tuple[str, str], str] = {}

        def set_password(service, credential_id, secret):
            stored[(service, credential_id)] = secret

        def get_password(service, credential_id):
            return stored.get((service, credential_id))

        def delete_password(service, credential_id):
            stored.pop((service, credential_id), None)

        with patch("impodo.adapters.protected_evidence.credential_vault.keyring") as keyring:
            keyring.set_password.side_effect = set_password
            keyring.get_password.side_effect = get_password
            keyring.delete_password.side_effect = delete_password
            current = CredentialVault()
            store_target_credential(
                current,
                self.workspace_state,
                TargetCredentialRole.READ,
                "persistent-read-key",
                persistent=True,
            )
            store_target_credential(
                current,
                self.workspace_state,
                TargetCredentialRole.READ,
                "session-replacement",
                persistent=False,
            )

            fresh = CredentialVault()
            status = get_target_credential_status(
                fresh,
                self.workspace_state,
                TargetCredentialRole.READ,
            )

        self.assertEqual(status.availability.value, "MISSING")


if __name__ == "__main__":
    unittest.main()

