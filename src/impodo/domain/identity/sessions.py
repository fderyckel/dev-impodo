"""Define opaque server-session lifecycle and expiry contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from impodo.domain.identity.bootstrap import SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind

SESSION_REJECTED_MESSAGE = "Session is not available"
_SESSION_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class ServerSessionError(RuntimeError):
    """Base class for bounded server-session failures."""


class ServerSessionInputError(ServerSessionError):
    """Report invalid internal session input without reproducing it."""


class ServerSessionRejectedError(ServerSessionError):
    """Return one generic rejection for invalid or unavailable sessions."""


class ServerSessionConflictError(ServerSessionError):
    """Reject a stale exact-session administrative mutation."""


class ServerSessionUnavailableError(ServerSessionError):
    """Report a retryable server-session persistence failure."""


class ServerSessionStorageError(ServerSessionError):
    """Report a non-retryable server-session persistence failure."""


class SessionAuthenticationMethod(StrEnum):
    """Name the strongest method established for one session."""

    PASSWORD = "PASSWORD"
    WEBAUTHN = "WEBAUTHN"


class SessionState(StrEnum):
    """Describe whether a stored session may authenticate a request."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class SessionRevocationReason(StrEnum):
    """Name bounded reasons that make a session unusable."""

    ADMINISTRATOR = "ADMINISTRATOR"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    EXPIRED = "EXPIRED"
    ORGANIZATION_SUSPENDED = "ORGANIZATION_SUSPENDED"
    PASSWORD_RESET = "PASSWORD_RESET"
    SECURITY_REVISION = "SECURITY_REVISION"


@dataclass(frozen=True, slots=True)
class ServerSessionPolicy:
    """Bound idle, absolute, and privilege-elevation lifetimes."""

    idle_timeout: timedelta = timedelta(minutes=30)
    absolute_timeout: timedelta = timedelta(hours=12)
    elevation_timeout: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if (
            self.idle_timeout <= timedelta(0)
            or self.absolute_timeout <= timedelta(0)
            or self.elevation_timeout <= timedelta(0)
            or self.idle_timeout > self.absolute_timeout
            or self.elevation_timeout > self.absolute_timeout
        ):
            raise ServerSessionInputError("Server session policy is invalid")


@dataclass(frozen=True, slots=True)
class SessionSubject:
    """Bind a session to one principal and at most one Organization."""

    principal_kind: LoginPrincipalKind
    principal_id: str
    organization_id: str | None

    def __post_init__(self) -> None:
        if not self.principal_id:
            raise ServerSessionInputError("Session subject is invalid")
        if self.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
            if self.organization_id is not None:
                raise ServerSessionInputError("Session subject is invalid")
        elif not self.organization_id:
            raise ServerSessionInputError("Session subject is invalid")


@dataclass(frozen=True, slots=True)
class SessionSecretMaterial:
    """Carry generated browser secrets without revealing them in repr."""

    session_id: str
    handle: str = field(repr=False)
    handle_digest: bytes = field(repr=False)
    csrf_token: str = field(repr=False)
    csrf_digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            _SESSION_ID_PATTERN.fullmatch(self.session_id) is None
            or not self.handle
            or not self.csrf_token
            or len(self.handle_digest) != 32
            or len(self.csrf_digest) != 32
        ):
            raise ServerSessionInputError("Session secret material is invalid")


@dataclass(frozen=True, slots=True)
class SessionProof:
    """Carry digests derived from a presented browser session."""

    session_id: str
    handle_digest: bytes = field(repr=False)
    csrf_digest: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            _SESSION_ID_PATTERN.fullmatch(self.session_id) is None
            or len(self.handle_digest) != 32
            or (
                self.csrf_digest is not None
                and len(self.csrf_digest) != 32
            )
        ):
            raise ServerSessionInputError("Session proof is invalid")


@dataclass(frozen=True, slots=True)
class CreateServerSessionCommand:
    """Create a new authenticated session without accepting an old handle."""

    subject: SessionSubject
    authentication_method: SessionAuthenticationMethod
    security_revision: int

    def __post_init__(self) -> None:
        if self.security_revision <= 0:
            raise ServerSessionInputError("Session security revision is invalid")


@dataclass(frozen=True, slots=True)
class ServerSessionRecord:
    """Describe the complete active session inserted atomically."""

    session_id: str
    subject: SessionSubject
    authentication_method: SessionAuthenticationMethod
    security_revision: int
    handle_digest: bytes = field(repr=False)
    csrf_digest: bytes = field(repr=False)
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    elevated_until: datetime | None
    optimistic_revision: int

    def __post_init__(self) -> None:
        if (
            _SESSION_ID_PATTERN.fullmatch(self.session_id) is None
            or self.security_revision <= 0
            or len(self.handle_digest) != 32
            or len(self.csrf_digest) != 32
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
            or self.last_seen_at != self.created_at
            or self.idle_expires_at <= self.created_at
            or self.absolute_expires_at < self.idle_expires_at
            or (
                self.elevated_until is not None
                and not (
                    self.created_at
                    < self.elevated_until
                    <= self.absolute_expires_at
                )
            )
            or self.optimistic_revision != 1
        ):
            raise ServerSessionInputError("Server session record is invalid")


@dataclass(frozen=True, slots=True)
class CurrentSessionView:
    """Return bounded current-session facts without stored digests."""

    session_id: str
    subject: SessionSubject
    authentication_method: SessionAuthenticationMethod
    security_revision: int
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    elevated_until: datetime | None
    optimistic_revision: int

    def __post_init__(self) -> None:
        if (
            _SESSION_ID_PATTERN.fullmatch(self.session_id) is None
            or self.security_revision <= 0
            or self.optimistic_revision <= 0
        ):
            raise ServerSessionStorageError("Current session view is invalid")

    def is_elevated_at(self, observed_at: datetime) -> bool:
        """Return whether fresh phishing-resistant elevation is still valid."""

        return self.elevated_until is not None and observed_at < self.elevated_until


@dataclass(frozen=True, slots=True)
class ServerSessionCredentials:
    """Return raw browser credentials once after the transaction commits."""

    handle: str = field(repr=False)
    csrf_token: str = field(repr=False)
    session: CurrentSessionView


@dataclass(frozen=True, slots=True)
class AccessServerSessionCommand:
    """Resolve a browser handle and optional unsafe-request CSRF token."""

    handle: str = field(repr=False)
    csrf_token: str | None = field(default=None, repr=False)
    require_csrf: bool = False

    def __post_init__(self) -> None:
        if self.require_csrf and not self.csrf_token:
            raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE)


@dataclass(frozen=True, slots=True)
class RotateServerSessionCommand:
    """Rotate one active handle after privilege elevation."""

    handle: str = field(repr=False)
    authentication_method: SessionAuthenticationMethod
    security_revision: int

    def __post_init__(self) -> None:
        if self.security_revision <= 0:
            raise ServerSessionInputError("Session security revision is invalid")


@dataclass(frozen=True, slots=True)
class RevokeServerSessionCommand:
    """Revoke an exact self-session under optimistic concurrency."""

    actor: SessionSubject
    session_id: str
    expected_revision: int
    reason: SessionRevocationReason = SessionRevocationReason.ADMINISTRATOR

    def __post_init__(self) -> None:
        if (
            _SESSION_ID_PATTERN.fullmatch(self.session_id) is None
            or self.expected_revision <= 0
        ):
            raise ServerSessionInputError("Session revocation command is invalid")


@dataclass(frozen=True, slots=True)
class SessionSecurityEvent:
    """Describe a session event without a handle, CSRF token, or digest."""

    event_id: str
    deployment_id: str
    kind: SecurityEventKind
    subject: SessionSubject | None
    actor: SessionSubject | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        if (
            not self.event_id
            or not self.deployment_id
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ServerSessionInputError("Session security event is invalid")


__all__ = [
    "SESSION_REJECTED_MESSAGE",
    "AccessServerSessionCommand",
    "CreateServerSessionCommand",
    "CurrentSessionView",
    "RevokeServerSessionCommand",
    "RotateServerSessionCommand",
    "ServerSessionConflictError",
    "ServerSessionCredentials",
    "ServerSessionError",
    "ServerSessionInputError",
    "ServerSessionPolicy",
    "ServerSessionRecord",
    "ServerSessionRejectedError",
    "ServerSessionStorageError",
    "ServerSessionUnavailableError",
    "SessionAuthenticationMethod",
    "SessionProof",
    "SessionRevocationReason",
    "SessionSecretMaterial",
    "SessionSecurityEvent",
    "SessionState",
    "SessionSubject",
]
