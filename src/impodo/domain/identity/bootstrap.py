"""Define the one-time first Platform Administrator bootstrap contract."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+")


class PlatformAdministratorBootstrapError(RuntimeError):
    """Base class for bounded first-operator bootstrap failures."""


class PlatformAdministratorBootstrapInputError(PlatformAdministratorBootstrapError):
    """Report invalid operator input without reproducing its value."""


class PlatformAdministratorAlreadyBootstrappedError(
    PlatformAdministratorBootstrapError
):
    """Refuse an ordinary bootstrap after an active operator exists."""


class PlatformAdministratorBootstrapUnavailableError(
    PlatformAdministratorBootstrapError
):
    """Report a retryable bootstrap persistence failure."""


class PlatformAdministratorBootstrapStorageError(PlatformAdministratorBootstrapError):
    """Report a non-retryable bootstrap persistence failure."""


class PlatformAdministratorStatus(StrEnum):
    """Describe whether a built-in platform operator may authenticate."""

    ACTIVE = "ACTIVE"
    DEACTIVATED = "DEACTIVATED"


class SecurityEventKind(StrEnum):
    """Name bounded platform identity security events."""

    PLATFORM_ADMINISTRATOR_BOOTSTRAPPED = "PLATFORM_ADMINISTRATOR_BOOTSTRAPPED"
    AUTHENTICATION_SUCCEEDED = "AUTHENTICATION_SUCCEEDED"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    PASSWORD_REHASHED = "PASSWORD_REHASHED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    RECOVERY_TOKEN_ISSUED = "RECOVERY_TOKEN_ISSUED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """Hold a slow password hash and its explicit rehash metadata."""

    algorithm: str
    parameters: tuple[tuple[str, int], ...]
    salt: bytes = field(repr=False)
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not self.algorithm
            or self.algorithm != self.algorithm.strip()
            or not self.parameters
            or not self.salt
            or not self.digest
        ):
            raise PlatformAdministratorBootstrapInputError(
                "Password hash metadata is invalid"
            )
        names = tuple(name for name, _value in self.parameters)
        if len(set(names)) != len(names) or any(
            not name or value <= 0 for name, value in self.parameters
        ):
            raise PlatformAdministratorBootstrapInputError(
                "Password hash metadata is invalid"
            )


@dataclass(frozen=True, slots=True)
class PlatformAdministratorBootstrapCommand:
    """Carry first-operator input without exposing the password in repr."""

    login_email: str
    display_name: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PlatformAdministratorBootstrapRecord:
    """Describe the active operator and password identity stored atomically."""

    administrator_id: str
    normalized_login_email: str
    display_name: str
    status: PlatformAdministratorStatus
    security_revision: int
    password_hash: PasswordHash = field(repr=False)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BootstrapSecurityEvent:
    """Describe the non-sensitive event appended with first-operator creation."""

    event_id: str
    deployment_id: str
    kind: SecurityEventKind
    subject_administrator_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PlatformAdministratorBootstrapResult:
    """Return only non-secret identities and time after bootstrap commits."""

    administrator_id: str
    security_event_id: str
    created_at: datetime


def normalize_login_email(value: str) -> str:
    """Return the canonical globally comparable login email."""

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if (
        not normalized
        or len(normalized) > 320
        or _EMAIL_PATTERN.fullmatch(normalized) is None
    ):
        raise PlatformAdministratorBootstrapInputError("Login email is invalid")
    return normalized


def validate_bootstrap_display_name(value: str) -> str:
    """Return one bounded operator-facing display name."""

    normalized = value.strip()
    if not normalized or len(normalized) > 200 or "\x00" in normalized:
        raise PlatformAdministratorBootstrapInputError("Display name is invalid")
    return normalized


def validate_bootstrap_password(value: str) -> str:
    """Require a bounded initial password without retaining derived metadata."""

    if not 16 <= len(value) <= 1024 or "\x00" in value:
        raise PlatformAdministratorBootstrapInputError(
            "Bootstrap password must contain between 16 and 1024 characters"
        )
    return value


__all__ = [
    "BootstrapSecurityEvent",
    "PasswordHash",
    "PlatformAdministratorAlreadyBootstrappedError",
    "PlatformAdministratorBootstrapCommand",
    "PlatformAdministratorBootstrapError",
    "PlatformAdministratorBootstrapInputError",
    "PlatformAdministratorBootstrapRecord",
    "PlatformAdministratorBootstrapResult",
    "PlatformAdministratorBootstrapStorageError",
    "PlatformAdministratorBootstrapUnavailableError",
    "PlatformAdministratorStatus",
    "SecurityEventKind",
    "normalize_login_email",
    "validate_bootstrap_display_name",
    "validate_bootstrap_password",
]
