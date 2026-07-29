"""Secret-store boundary for Odoo API keys."""

from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError


SERVICE_NAME = "Impodo Odoo read-only"


class SecretStoreError(RuntimeError):
    """Raised when the operating-system credential store is unavailable."""


class SecretStore(Protocol):
    def get(self, project_id: str) -> str | None: ...

    def set(self, project_id: str, secret: str, *, persistent: bool) -> None: ...

    def delete(self, project_id: str) -> None: ...


class CredentialVault:
    """Keep session secrets in memory and optional persistent secrets in keyring."""

    def __init__(self) -> None:
        self._session: dict[str, str] = {}

    def get(self, project_id: str) -> str | None:
        if project_id in self._session:
            return self._session[project_id]
        try:
            return keyring.get_password(SERVICE_NAME, project_id)
        except KeyringError as error:
            raise SecretStoreError(
                "Windows Credential Manager is unavailable"
            ) from error

    def set(self, project_id: str, secret: str, *, persistent: bool) -> None:
        clean_secret = secret.strip()
        if not clean_secret:
            raise SecretStoreError("API key is empty")
        self._session[project_id] = clean_secret
        if persistent:
            try:
                keyring.set_password(SERVICE_NAME, project_id, clean_secret)
            except KeyringError as error:
                raise SecretStoreError(
                    "Could not save the API key in Windows Credential Manager"
                ) from error

    def delete(self, project_id: str) -> None:
        self._session.pop(project_id, None)
        try:
            if keyring.get_password(SERVICE_NAME, project_id) is not None:
                keyring.delete_password(SERVICE_NAME, project_id)
        except KeyringError as error:
            raise SecretStoreError(
                "Could not delete the API key from Windows Credential Manager"
            ) from error


class MemorySecretStore:
    """Deterministic secret store for tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, project_id: str) -> str | None:
        return self.values.get(project_id)

    def set(self, project_id: str, secret: str, *, persistent: bool) -> None:
        del persistent
        self.values[project_id] = secret

    def delete(self, project_id: str) -> None:
        self.values.pop(project_id, None)
