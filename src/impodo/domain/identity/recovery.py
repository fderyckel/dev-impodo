"""Define secret-safe one-time account-recovery contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from impodo.domain.identity.authentication import (
    AuthenticationThrottlePolicy,
    validate_authentication_digest,
)
from impodo.domain.identity.bootstrap import PasswordHash

PASSWORD_RESET_ACCEPTED_MESSAGE = (
    "If account recovery is available, the request has been accepted"
)
PASSWORD_RESET_REJECTED_MESSAGE = "Account recovery did not complete"
_TOKEN_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class AccountRecoveryError(RuntimeError):
    """Base class for bounded account-recovery failures."""


class AccountRecoveryInputError(AccountRecoveryError):
    """Report invalid internal recovery input without reproducing it."""


class AccountRecoveryRejectedError(AccountRecoveryError):
    """Return one generic rejection for every public consumption failure."""


class AccountRecoverySubjectNotEligibleError(AccountRecoveryError):
    """Reject privileged issuance for an exact ineligible subject."""


class AccountRecoveryUnavailableError(AccountRecoveryError):
    """Report a retryable account-recovery persistence failure."""


class AccountRecoveryStorageError(AccountRecoveryError):
    """Report a non-retryable account-recovery persistence failure."""


class LoginPrincipalKind(StrEnum):
    """Name principal kinds with a globally unique login identity."""

    PLATFORM_ADMINISTRATOR = "PLATFORM_ADMINISTRATOR"


class RecoveryTokenPurpose(StrEnum):
    """Bind a one-time token to exactly one permitted transition."""

    ACTIVATION = "ACTIVATION"
    PASSWORD_RESET = "PASSWORD_RESET"


class RecoveryTokenState(StrEnum):
    """Describe the irreversible lifecycle of a one-time token."""

    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class RecoveryTokenPolicy:
    """Bound token lifetime and reset-request throttling."""

    activation_lifetime: timedelta = timedelta(hours=24)
    password_reset_lifetime: timedelta = timedelta(minutes=30)
    reset_throttle: AuthenticationThrottlePolicy = field(
        default_factory=lambda: AuthenticationThrottlePolicy(
            maximum_failures=5,
            failure_window=timedelta(minutes=30),
            lockout_duration=timedelta(minutes=30),
        )
    )

    def __post_init__(self) -> None:
        if (
            self.activation_lifetime <= timedelta(0)
            or self.password_reset_lifetime <= timedelta(0)
        ):
            raise AccountRecoveryInputError("Recovery token policy is invalid")

    def lifetime_for(self, purpose: RecoveryTokenPurpose) -> timedelta:
        """Return the fixed lifetime for one purpose."""

        if purpose is RecoveryTokenPurpose.ACTIVATION:
            return self.activation_lifetime
        return self.password_reset_lifetime


@dataclass(frozen=True, slots=True)
class RecoveryTokenMaterial:
    """Carry generated token material without revealing it in representations."""

    token_id: str
    token: str = field(repr=False)
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            _TOKEN_ID_PATTERN.fullmatch(self.token_id) is None
            or not self.token
            or len(self.digest) != 32
        ):
            raise AccountRecoveryInputError("Recovery token material is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryTokenProof:
    """Carry the selector and digest derived from a presented token."""

    token_id: str
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if _TOKEN_ID_PATTERN.fullmatch(self.token_id) is None or len(self.digest) != 32:
            raise AccountRecoveryInputError("Recovery token proof is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryTokenIssue:
    """Describe the persisted half of one purpose-bound token."""

    token_id: str
    purpose: RecoveryTokenPurpose
    digest: bytes = field(repr=False)
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            _TOKEN_ID_PATTERN.fullmatch(self.token_id) is None
            or len(self.digest) != 32
            or self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at <= self.issued_at
        ):
            raise AccountRecoveryInputError("Recovery token issue is invalid")


@dataclass(frozen=True, slots=True)
class RecoverySecurityEvent:
    """Describe an account-recovery event without token material."""

    event_id: str
    deployment_id: str
    kind: str
    subject_administrator_id: str | None
    principal_hint_digest: str
    source_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.event_id or not self.deployment_id:
            raise AccountRecoveryInputError("Recovery security event is invalid")
        validate_authentication_digest(self.principal_hint_digest)
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class PasswordResetRequestCommand:
    """Carry a public reset request without promising account existence."""

    login_email: str
    source_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PasswordResetRequestResult:
    """Return the one constant public reset-request response."""

    message: str = PASSWORD_RESET_ACCEPTED_MESSAGE


@dataclass(frozen=True, slots=True)
class PasswordResetDelivery:
    """Carry an internal delivery target and one-time raw token."""

    normalized_login_email: str
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedRecoveryTokenCommand:
    """Carry a privileged exact-subject issuance request."""

    principal_kind: LoginPrincipalKind
    principal_id: str
    purpose: RecoveryTokenPurpose
    source_digest: str

    def __post_init__(self) -> None:
        if not self.principal_id:
            raise AccountRecoveryInputError("Recovery token subject is invalid")
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class AuthorizedRecoveryTokenResult:
    """Return a raw token once, only after authorized issuance commits."""

    purpose: RecoveryTokenPurpose
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PasswordResetCommand:
    """Carry one token consumption without exposing either secret in repr."""

    token: str = field(repr=False)
    new_password: str = field(repr=False)
    source_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PasswordResetMutation:
    """Describe one atomic password and session-invalidating transition."""

    proof: RecoveryTokenProof
    replacement_hash: PasswordHash = field(repr=False)
    source_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class PasswordResetResult:
    """Return only non-secret facts after the transaction commits."""

    completed_at: datetime


__all__ = [
    "PASSWORD_RESET_ACCEPTED_MESSAGE",
    "PASSWORD_RESET_REJECTED_MESSAGE",
    "AccountRecoveryError",
    "AccountRecoveryInputError",
    "AccountRecoveryRejectedError",
    "AccountRecoveryStorageError",
    "AccountRecoverySubjectNotEligibleError",
    "AccountRecoveryUnavailableError",
    "AuthorizedRecoveryTokenCommand",
    "AuthorizedRecoveryTokenResult",
    "LoginPrincipalKind",
    "PasswordResetCommand",
    "PasswordResetDelivery",
    "PasswordResetMutation",
    "PasswordResetRequestCommand",
    "PasswordResetRequestResult",
    "PasswordResetResult",
    "RecoverySecurityEvent",
    "RecoveryTokenIssue",
    "RecoveryTokenMaterial",
    "RecoveryTokenPolicy",
    "RecoveryTokenProof",
    "RecoveryTokenPurpose",
    "RecoveryTokenState",
]
