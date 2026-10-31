"""Declare account-recovery cryptography, delivery, and persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.application.identity.authentication_ports import (
    PasswordAuthenticationPolicy,
)
from impodo.domain.identity.recovery import (
    AuthorizedRecoveryTokenCommand,
    LoginPrincipalKind,
    PasswordResetDelivery,
    PasswordResetMutation,
    RecoverySecurityEvent,
    RecoveryTokenIssue,
    RecoveryTokenMaterial,
    RecoveryTokenPolicy,
    RecoveryTokenProof,
    RecoveryTokenPurpose,
)


class RecoveryTokenFactory(Protocol):
    """Create and prove purpose-bound high-entropy one-time tokens."""

    def create(self, purpose: RecoveryTokenPurpose) -> RecoveryTokenMaterial: ...

    def prove(
        self,
        token: str,
        purpose: RecoveryTokenPurpose,
    ) -> RecoveryTokenProof: ...


class IdentifierDigester(Protocol):
    """Key low-entropy account and source digests by deployment."""

    def digest(self, value: str, *, scope: str) -> str: ...


class PasswordResetDeliveryPort(Protocol):
    """Deliver a raw reset token without returning it to the public caller."""

    def deliver(self, delivery: PasswordResetDelivery) -> None: ...


class PasswordResetDeliveryError(RuntimeError):
    """Report a bounded internal delivery failure to the reset service."""


class AccountRecoveryRepository(Protocol):
    """Own short atomic recovery-token and password transitions."""

    def inspect_token_principal(
        self, proof: RecoveryTokenProof, purpose: RecoveryTokenPurpose, *, observed_at: datetime,
    ) -> LoginPrincipalKind | None: ...

    def request_password_reset(
        self,
        *,
        normalized_login_email: str,
        account_digest: str,
        source_digest: str,
        issue: RecoveryTokenIssue,
        request_event: RecoverySecurityEvent,
        issue_event: RecoverySecurityEvent,
        policy: RecoveryTokenPolicy,
    ) -> bool: ...

    def issue_authorized(
        self,
        command: AuthorizedRecoveryTokenCommand,
        issue: RecoveryTokenIssue,
        event: RecoverySecurityEvent,
    ) -> None: ...

    def reset_password(
        self,
        mutation: PasswordResetMutation,
        event: RecoverySecurityEvent,
    ) -> bool: ...


PasswordResetHasher = PasswordAuthenticationPolicy


__all__ = [
    "AccountRecoveryRepository",
    "IdentifierDigester",
    "PasswordResetDeliveryError",
    "PasswordResetDeliveryPort",
    "PasswordResetHasher",
    "RecoveryTokenFactory",
]
