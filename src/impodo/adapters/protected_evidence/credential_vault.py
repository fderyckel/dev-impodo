"""Secret-store boundary for Odoo API keys and other target credentials.

Routes address secrets by opaque project-derived credential IDs. Application
and domain objects never receive persistent-store details, and repositories do
not serialize secret values. ``CredentialVault`` keeps session values in
memory and optionally mirrors them to the operating-system keyring.
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

from impodo.application.shared.secrets import (
    DESTINATION_READ_SERVICE_NAME,
    PROTECTED_EVIDENCE_SERVICE_NAME,
    READ_SERVICE_NAME,
    WRITE_SERVICE_NAME,
    SecretStoreError,
)


class CredentialVault:
    """Keep session secrets in memory and optional persistent secrets in keyring."""

    def __init__(self) -> None:
        self._session: dict[str, str] = {}

    def get(self, credential_id: str) -> str | None:
        """Prefer the session copy, then consult the operating-system keyring."""

        if credential_id in self._session:
            return self._session[credential_id]
        try:
            return keyring.get_password(
                _service_name(credential_id),
                credential_id,
            )
        except KeyringError as error:
            raise SecretStoreError(
                "Windows Credential Manager is unavailable"
            ) from error

    def set(self, credential_id: str, secret: str, *, persistent: bool) -> None:
        """Validate and retain a secret, optionally persisting it in keyring."""

        clean_secret = secret.strip()
        if not clean_secret:
            raise SecretStoreError("API key is empty")
        if persistent:
            try:
                keyring.set_password(
                    _service_name(credential_id),
                    credential_id,
                    clean_secret,
                )
            except KeyringError as error:
                raise SecretStoreError(
                    "Could not save the API key in Windows Credential Manager"
                ) from error
        else:
            try:
                service_name = _service_name(credential_id)
                if keyring.get_password(service_name, credential_id) is not None:
                    keyring.delete_password(service_name, credential_id)
            except KeyringError as error:
                raise SecretStoreError(
                    "Could not remove the earlier API key from Windows "
                    "Credential Manager"
                ) from error
        self._session[credential_id] = clean_secret

    def delete(self, credential_id: str) -> None:
        """Delete the in-memory and operating-system copies of a credential."""

        self._session.pop(credential_id, None)
        try:
            service_name = _service_name(credential_id)
            if keyring.get_password(service_name, credential_id) is not None:
                keyring.delete_password(service_name, credential_id)
        except KeyringError as error:
            raise SecretStoreError(
                "Could not delete the API key from Windows Credential Manager"
            ) from error


class MemorySecretStore:
    """Deterministic secret store for tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, credential_id: str) -> str | None:
        """Return a test secret from process-local memory."""

        return self.values.get(credential_id)

    def set(self, credential_id: str, secret: str, *, persistent: bool) -> None:
        """Store a test secret; persistence has no separate meaning here."""

        del persistent
        self.values[credential_id] = secret

    def delete(self, credential_id: str) -> None:
        """Remove a test secret if present."""

        self.values.pop(credential_id, None)


def _service_name(credential_id: str) -> str:
    """Route one current role-qualified credential ID."""

    parts = credential_id.rsplit(":", 2)
    if len(parts) != 3:
        raise SecretStoreError("Odoo credential identifier is invalid")
    if parts[1] == "write":
        return WRITE_SERVICE_NAME
    if parts[1] == "read":
        return READ_SERVICE_NAME
    if parts[1] == "destination_read":
        return DESTINATION_READ_SERVICE_NAME
    if parts[1] == "protected":
        return PROTECTED_EVIDENCE_SERVICE_NAME
    raise SecretStoreError("Odoo credential role is invalid")
