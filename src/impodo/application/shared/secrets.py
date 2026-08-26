"""Application-facing port for opaque target and evidence credentials."""

from __future__ import annotations

from typing import Protocol


READ_SERVICE_NAME = "Impodo Odoo read-only"
WRITE_SERVICE_NAME = "Impodo Odoo write"
PROTECTED_EVIDENCE_SERVICE_NAME = "Impodo protected Odoo evidence"


class SecretStoreError(RuntimeError):
    """Raised when the configured credential store is unavailable."""


class SecretStore(Protocol):
    """Retrieve, set, and delete a credential through an opaque identity."""

    def get(self, credential_id: str) -> str | None: ...

    def set(self, credential_id: str, secret: str, *, persistent: bool) -> None: ...

    def delete(self, credential_id: str) -> None: ...
