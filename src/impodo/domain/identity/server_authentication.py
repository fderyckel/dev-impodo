"""Define generic, secret-safe shared-server authentication contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from impodo.domain.identity.authentication import validate_authentication_digest
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind

SERVER_AUTHENTICATION_REJECTED_MESSAGE = "Authentication did not complete"


class ServerAuthenticationError(RuntimeError):
    """Base class for generic shared-server login failures."""


class ServerAuthenticationRejectedError(ServerAuthenticationError):
    """Return one generic rejection for every public login failure."""


class ServerAuthenticationStorageError(ServerAuthenticationError):
    """Report a non-retryable generic authentication persistence failure."""


class ServerAuthenticationUnavailableError(ServerAuthenticationError):
    """Report a retryable generic authentication persistence failure."""


@dataclass(frozen=True, slots=True)
class ServerAuthenticationCommand:
    """Carry one global login attempt without exposing its password."""

    login_email: str
    password: str = field(repr=False)
    source_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServerAuthenticationCredential:
    """Hold one current credential and its permanent tenant binding."""

    principal_kind: LoginPrincipalKind
    principal_id: str
    organization_id: str | None
    eligible: bool
    security_revision: int
    password_hash: PasswordHash = field(repr=False)
    password_revision: int

    def __post_init__(self) -> None:
        if (
            not self.principal_id
            or self.security_revision <= 0
            or self.password_revision <= 0
            or (
                self.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR
                and self.organization_id is not None
            )
            or (
                self.principal_kind
                in {LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR, LoginPrincipalKind.CUSTOMER_USER}
                and not self.organization_id
            )
        ):
            raise ServerAuthenticationStorageError(
                "Server authentication credential is invalid"
            )


@dataclass(frozen=True, slots=True)
class ServerAuthenticationAttempt:
    """Describe one global credential lookup without account disclosure."""

    account_digest: str
    source_digest: str
    credential: ServerAuthenticationCredential | None = field(repr=False)
    throttled: bool

    def __post_init__(self) -> None:
        validate_authentication_digest(self.account_digest)
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class ServerAuthenticationSecurityEvent:
    """Describe one digest-only login event for any supported principal."""

    event_id: str
    deployment_id: str
    kind: SecurityEventKind
    principal_kind: LoginPrincipalKind | None
    principal_id: str | None
    organization_id: str | None
    principal_hint_digest: str
    source_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if (
            not self.event_id
            or not self.deployment_id
            or self.kind
            not in {
                SecurityEventKind.AUTHENTICATION_SUCCEEDED,
                SecurityEventKind.AUTHENTICATION_REJECTED,
                SecurityEventKind.PASSWORD_REHASHED,
            }
            or (self.principal_kind is None) != (self.principal_id is None)
            or (
                self.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR
                and self.organization_id is not None
            )
            or (
                self.principal_kind
                in {LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR, LoginPrincipalKind.CUSTOMER_USER}
                and not self.organization_id
            )
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ServerAuthenticationStorageError(
                "Server authentication event is invalid"
            )
        validate_authentication_digest(self.principal_hint_digest)
        validate_authentication_digest(self.source_digest)


@dataclass(frozen=True, slots=True)
class ServerAuthenticationResult:
    """Return the verified identity facts needed for a bound session."""

    principal_kind: LoginPrincipalKind
    principal_id: str
    organization_id: str | None
    security_revision: int
    authenticated_at: datetime
    password_rehashed: bool


__all__ = [
    "SERVER_AUTHENTICATION_REJECTED_MESSAGE",
    "ServerAuthenticationAttempt",
    "ServerAuthenticationCommand",
    "ServerAuthenticationCredential",
    "ServerAuthenticationError",
    "ServerAuthenticationRejectedError",
    "ServerAuthenticationResult",
    "ServerAuthenticationSecurityEvent",
    "ServerAuthenticationStorageError",
    "ServerAuthenticationUnavailableError",
]
