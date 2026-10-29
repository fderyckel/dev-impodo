"""Declare first-operator hashing and persistence ports."""

from __future__ import annotations

from typing import Protocol

from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorBootstrapRecord,
)


class PasswordHasher(Protocol):
    """Derive one slow password hash without retaining the raw secret."""

    def hash(self, password: str) -> PasswordHash: ...


class PlatformAdministratorBootstrapRepository(Protocol):
    """Create the first operator and its security event atomically."""

    def bootstrap(
        self,
        record: PlatformAdministratorBootstrapRecord,
        event: BootstrapSecurityEvent,
    ) -> None: ...


__all__ = ["PasswordHasher", "PlatformAdministratorBootstrapRepository"]
