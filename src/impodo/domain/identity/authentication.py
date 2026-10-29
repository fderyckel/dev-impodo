"""Define secret-safe platform authentication records and policy facts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorStatus,
)

AUTHENTICATION_REJECTED_MESSAGE = "Authentication did not complete"
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class PlatformAuthenticationError(RuntimeError):
    """Base class for platform-login failures."""


class PlatformAuthenticationInputError(PlatformAuthenticationError):
    """Report invalid internal authentication input without its value."""


class PlatformAuthenticationRejectedError(PlatformAuthenticationError):
    """Return one generic rejection for every public login failure."""


class PlatformAuthenticationUnavailableError(PlatformAuthenticationError):
    """Report a retryable authentication persistence failure."""


class PlatformAuthenticationStorageError(PlatformAuthenticationError):
    """Report a non-retryable authentication persistence failure."""


@dataclass(frozen=True, slots=True)
class AuthenticationThrottlePolicy:
    """Bound failed attempts without making account existence observable."""

    maximum_failures: int = 5
    failure_window: timedelta = timedelta(minutes=15)
    lockout_duration: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if (
            self.maximum_failures <= 0
            or self.failure_window <= timedelta(0)
            or self.lockout_duration <= timedelta(0)
        ):
            raise PlatformAuthenticationInputError(
                "Authentication throttle policy is invalid"
            )


@dataclass(frozen=True, slots=True)
class PlatformAuthenticationCommand:
    """Carry one login attempt without exposing its password in repr."""

    login_email: str
    password: str = field(repr=False)
    source_digest: str


@dataclass(frozen=True, slots=True)
class PlatformAuthenticationCredential:
    """Hold the current platform credential needed for one verification."""

    administrator_id: str
    status: PlatformAdministratorStatus
    security_revision: int
    password_hash: PasswordHash = field(repr=False)
    password_revision: int

    def __post_init__(self) -> None:
        if self.security_revision <= 0 or self.password_revision <= 0:
            raise PlatformAuthenticationStorageError(
                "Platform authentication credential is invalid"
            )


@dataclass(frozen=True, slots=True)
class PlatformAuthenticationAttempt:
    """Describe one credential lookup without revealing account existence."""

    account_digest: str
    source_digest: str
    credential: PlatformAuthenticationCredential | None = field(repr=False)
    throttled: bool

    def __post_init__(self) -> None:
        validate_authentication_digest(self.account_digest)
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class AuthenticationSecurityEvent:
    """Describe a bounded login event without a raw identifier or source."""

    event_id: str
    deployment_id: str
    kind: str
    subject_administrator_id: str | None
    principal_hint_digest: str
    source_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.event_id or not self.deployment_id:
            raise PlatformAuthenticationInputError(
                "Authentication security event is invalid"
            )
        validate_authentication_digest(self.principal_hint_digest)
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class PlatformAuthenticationResult:
    """Return the verified principal facts needed to create a session later."""

    administrator_id: str
    security_revision: int
    authenticated_at: datetime
    password_rehashed: bool


def authentication_identifier_digest(value: str) -> str:
    """Return a bounded non-raw digest for throttling and security events."""

    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def validate_authentication_digest(value: str) -> str:
    """Require the one-way digest boundary supplied by request composition."""

    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise PlatformAuthenticationInputError(
            "Authentication source digest is invalid"
        )
    return value


__all__ = [
    "AUTHENTICATION_REJECTED_MESSAGE",
    "AuthenticationSecurityEvent",
    "AuthenticationThrottlePolicy",
    "PlatformAuthenticationAttempt",
    "PlatformAuthenticationCommand",
    "PlatformAuthenticationCredential",
    "PlatformAuthenticationError",
    "PlatformAuthenticationInputError",
    "PlatformAuthenticationRejectedError",
    "PlatformAuthenticationResult",
    "PlatformAuthenticationStorageError",
    "PlatformAuthenticationUnavailableError",
    "authentication_identifier_digest",
    "validate_authentication_digest",
]
