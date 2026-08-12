from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from impodo.projects import MigrationProject, OdooConnectionMode
from impodo.secrets import (
    CredentialVault,
    MemorySecretStore,
    READ_SERVICE_NAME,
    SecretStoreError,
    WRITE_SERVICE_NAME,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    delete_target_credentials,
    get_target_credential,
    local_read_credential_binding_hash,
    store_target_credential,
    target_read_credential_id,
    target_write_credential_id,
)


class TargetCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemorySecretStore()
        self.project = MigrationProject(
            project_id="project-1",
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
            self.project,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )

        self.assertNotEqual(
            target_read_credential_id(self.project),
            target_write_credential_id(self.project),
        )
        self.assertEqual(
            get_target_credential(
                self.store,
                self.project,
                TargetCredentialRole.READ,
            ),
            read,
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                self.project,
                TargetCredentialRole.WRITE,
            )
        )
        payload = json.loads(
            self.store.values[target_read_credential_id(self.project)]
        )
        self.assertEqual(payload["role"], "READ")
        self.assertEqual(payload["secret"], "read-secret")
        self.assertRegex(read.binding_hash, r"^sha256:[0-9a-f]{64}$")

    def test_write_credential_never_falls_back_to_read_secret(
        self,
    ) -> None:
        store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                self.project,
                TargetCredentialRole.WRITE,
            )
        )

    def test_rotation_changes_only_that_roles_safe_binding(self) -> None:
        first = store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "same-secret",
            persistent=False,
        )
        second = store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "same-secret",
            persistent=False,
        )

        self.assertNotEqual(first.binding_hash, second.binding_hash)
        self.assertNotIn("same-secret", first.binding_hash)
        self.assertFalse(first.replaced)
        self.assertTrue(second.replaced)

    def test_target_change_does_not_reuse_a_stored_role(self) -> None:
        store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        changed = replace(self.project, odoo_database="staging")

        self.assertNotEqual(
            target_read_credential_id(self.project),
            target_read_credential_id(changed),
        )
        self.assertIsNone(
            get_target_credential(
                self.store,
                changed,
                TargetCredentialRole.READ,
            )
        )

    def test_invalid_role_envelope_is_rejected(self) -> None:
        write = store_target_credential(
            self.store,
            self.project,
            TargetCredentialRole.WRITE,
            "write-secret",
            persistent=False,
        )
        self.assertRegex(write.binding_hash, r"^sha256:[0-9a-f]{64}$")
        credential_id = target_write_credential_id(self.project)
        payload = json.loads(self.store.values[credential_id])
        payload["role"] = "READ"
        self.store.values[credential_id] = json.dumps(payload)

        with self.assertRaisesRegex(SecretStoreError, "does not match"):
            get_target_credential(
                self.store,
                self.project,
                TargetCredentialRole.WRITE,
            )

    def test_delete_removes_both_current_roles(self) -> None:
        for role, secret in (
            (TargetCredentialRole.READ, "read-secret"),
            (TargetCredentialRole.WRITE, "write-secret"),
        ):
            store_target_credential(
                self.store,
                self.project,
                role,
                secret,
                persistent=False,
            )
        delete_target_credentials(self.store, self.project)

        self.assertEqual(self.store.values, {})

    def test_local_no_key_binding_is_stable_and_target_bound(self) -> None:
        local = replace(
            self.project,
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
        read_id = target_read_credential_id(self.project)
        write_id = target_write_credential_id(self.project)

        with patch("impodo.secrets.keyring") as keyring:
            vault.set(read_id, "read-secret", persistent=True)
            vault.set(write_id, "write-secret", persistent=True)
            fresh_vault = CredentialVault()
            fresh_vault.get(read_id)
            fresh_vault.get(write_id)

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
        keyring.get_password.assert_any_call(READ_SERVICE_NAME, read_id)
        keyring.get_password.assert_any_call(WRITE_SERVICE_NAME, write_id)


if __name__ == "__main__":
    unittest.main()
