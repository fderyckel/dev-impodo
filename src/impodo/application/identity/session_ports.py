"""Declare opaque session-secret and transactional persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.identity.sessions import (
    CurrentSessionView,
    RevokeServerSessionCommand,
    ServerSessionRecord,
    SessionAuthenticationMethod,
    SessionProof,
    SessionSecretMaterial,
    SessionSecurityEvent,
)


class SessionSecretFactory(Protocol):
    """Generate or prove high-entropy browser session credentials."""

    def create(self) -> SessionSecretMaterial: ...

    def rotate(self, session_id: str) -> SessionSecretMaterial: ...

    def prove(self, handle: str, csrf_token: str | None = None) -> SessionProof: ...


class ServerSessionRepository(Protocol):
    """Own one short transaction for each server-session transition."""

    def create(
        self,
        record: ServerSessionRecord,
        event: SessionSecurityEvent,
    ) -> None: ...

    def resolve(
        self,
        *,
        proof: SessionProof,
        accessed_at: datetime,
        proposed_idle_expires_at: datetime,
    ) -> CurrentSessionView | None: ...

    def rotate(
        self,
        *,
        proof: SessionProof,
        replacement: SessionSecretMaterial,
        authentication_method: SessionAuthenticationMethod,
        expected_security_revision: int,
        rotated_at: datetime,
        proposed_idle_expires_at: datetime,
        elevated_until: datetime,
        event: SessionSecurityEvent,
    ) -> CurrentSessionView | None: ...

    def revoke(
        self,
        command: RevokeServerSessionCommand,
        event: SessionSecurityEvent,
        *,
        revoked_at: datetime,
    ) -> None: ...


__all__ = ["ServerSessionRepository", "SessionSecretFactory"]
