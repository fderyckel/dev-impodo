"""Define tenant-scoped Organization and customer-account administration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from impodo.domain.identity.bootstrap import PasswordHash
from impodo.domain.identity.recovery import LoginPrincipalKind, RecoveryTokenProof

_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_ORGANIZATION_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,198}[a-z0-9])?")


class IdentityAdministrationError(RuntimeError):
    """Base class for bounded Organization administration failures."""


class IdentityAdministrationInputError(IdentityAdministrationError):
    """Reject invalid internal input without reproducing customer values."""


class IdentityAdministrationNotFoundError(IdentityAdministrationError):
    """Hide absent and cross-Organization administration targets alike."""


class IdentityAdministrationConflictError(IdentityAdministrationError):
    """Reject a stale or uniqueness-conflicting exact mutation."""


class IdentityAdministrationUnavailableError(IdentityAdministrationError):
    """Report a retryable administration persistence failure."""


class IdentityAdministrationStorageError(IdentityAdministrationError):
    """Report a non-retryable administration persistence failure."""


class OrganizationStatus(StrEnum):
    """Describe whether an Organization can admit customer sessions."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class CustomerUserStatus(StrEnum):
    """Describe the irreversible and reversible customer-account lifecycle."""

    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DEACTIVATED = "DEACTIVATED"


class AdministrationEventKind(StrEnum):
    """Name bounded Organization and customer-account security events."""

    ORGANIZATION_CREATED = "ORGANIZATION_CREATED"
    ORGANIZATION_RENAMED = "ORGANIZATION_RENAMED"
    ORGANIZATION_SUSPENDED = "ORGANIZATION_SUSPENDED"
    ORGANIZATION_REACTIVATED = "ORGANIZATION_REACTIVATED"
    ORGANIZATION_POLICY_REVISED = "ORGANIZATION_POLICY_REVISED"
    ORGANIZATION_ADMINISTRATOR_ASSIGNED = "ORGANIZATION_ADMINISTRATOR_ASSIGNED"
    ORGANIZATION_ADMINISTRATOR_REPLACED = "ORGANIZATION_ADMINISTRATOR_REPLACED"
    CUSTOMER_USER_CREATED = "CUSTOMER_USER_CREATED"
    CUSTOMER_USER_ACTIVATED = "CUSTOMER_USER_ACTIVATED"
    CUSTOMER_USER_SUSPENDED = "CUSTOMER_USER_SUSPENDED"
    CUSTOMER_USER_REACTIVATED = "CUSTOMER_USER_REACTIVATED"
    CUSTOMER_USER_DEACTIVATED = "CUSTOMER_USER_DEACTIVATED"
    CUSTOMER_RESET_TOKEN_ISSUED = "CUSTOMER_RESET_TOKEN_ISSUED"
    CUSTOMER_PASSWORD_RESET = "CUSTOMER_PASSWORD_RESET"
    CUSTOMER_SESSION_REVOKED = "CUSTOMER_SESSION_REVOKED"


@dataclass(frozen=True, slots=True)
class AdministrationActor:
    """Bind an administrative actor to its permanent tenant scope."""

    principal_kind: LoginPrincipalKind
    principal_id: str
    organization_id: str | None

    def __post_init__(self) -> None:
        _validate_uuid(self.principal_id)
        if self.principal_kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
            if self.organization_id is not None:
                raise IdentityAdministrationInputError("Administration actor is invalid")
        elif self.principal_kind is LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR:
            _validate_organization_id(self.organization_id)
        else:
            raise IdentityAdministrationInputError("Administration actor is invalid")


@dataclass(frozen=True, slots=True)
class CreateOrganizationCommand:
    """Create one empty Organization under a Platform Administrator."""

    actor: AdministrationActor
    organization_id: str
    display_name: str

    def __post_init__(self) -> None:
        if self.actor.principal_kind is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
            raise IdentityAdministrationInputError("Organization command is invalid")
        _validate_organization_id(self.organization_id)
        _validate_display_name(self.display_name)


@dataclass(frozen=True, slots=True)
class OrganizationMutationCommand:
    """Mutate one exact Organization under optimistic concurrency."""

    actor: AdministrationActor
    organization_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        if self.actor.principal_kind is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
            raise IdentityAdministrationInputError("Organization command is invalid")
        _validate_organization_id(self.organization_id)
        if self.expected_revision <= 0:
            raise IdentityAdministrationInputError("Organization revision is invalid")


@dataclass(frozen=True, slots=True)
class RenameOrganizationCommand:
    """Rename one exact Organization."""

    mutation: OrganizationMutationCommand
    display_name: str

    def __post_init__(self) -> None:
        _validate_display_name(self.display_name)


@dataclass(frozen=True, slots=True)
class ReviseOrganizationPolicyCommand:
    """Advance one Organization policy revision without carrying policy payload."""

    mutation: OrganizationMutationCommand
    expected_policy_revision: int

    def __post_init__(self) -> None:
        if self.expected_policy_revision <= 0:
            raise IdentityAdministrationInputError("Policy revision is invalid")


@dataclass(frozen=True, slots=True)
class CreateCustomerUserCommand:
    """Create one pending customer account in the actor's Organization."""

    actor: AdministrationActor
    organization_id: str
    login_email: str
    display_name: str
    principal_kind: LoginPrincipalKind = LoginPrincipalKind.CUSTOMER_USER

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        _validate_display_name(self.display_name)
        if (
            self.principal_kind is not LoginPrincipalKind.CUSTOMER_USER
            or self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        ):
            raise IdentityAdministrationInputError("Customer account kind is invalid")
        if self.actor.organization_id != self.organization_id:
            raise IdentityAdministrationNotFoundError("Customer account is not available")


@dataclass(frozen=True, slots=True)
class ReplaceOrganizationAdministratorCommand:
    """Create and assign one replacement Organization Administrator."""

    actor: AdministrationActor
    organization_id: str
    login_email: str
    display_name: str
    expected_organization_revision: int

    def __post_init__(self) -> None:
        if self.actor.principal_kind is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
            raise IdentityAdministrationInputError("Administrator replacement is invalid")
        _validate_organization_id(self.organization_id)
        _validate_display_name(self.display_name)
        if self.expected_organization_revision <= 0:
            raise IdentityAdministrationInputError("Organization revision is invalid")


@dataclass(frozen=True, slots=True)
class CustomerUserMutationCommand:
    """Mutate one exact customer account inside one Organization."""

    actor: AdministrationActor
    organization_id: str
    user_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        _validate_uuid(self.user_id)
        if (
            self.expected_revision <= 0
            or self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        ):
            raise IdentityAdministrationInputError("Customer account revision is invalid")
        if self.actor.organization_id != self.organization_id:
            raise IdentityAdministrationNotFoundError("Customer account is not available")


@dataclass(frozen=True, slots=True)
class CustomerUserRecord:
    """Describe one pending customer account stored with its login identity."""

    user_id: str
    organization_id: str
    normalized_login_email: str
    display_name: str
    principal_kind: LoginPrincipalKind
    status: CustomerUserStatus
    security_revision: int
    optimistic_revision: int
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.user_id)
        _validate_organization_id(self.organization_id)
        _validate_display_name(self.display_name)
        if (
            self.principal_kind
            not in {
                LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
                LoginPrincipalKind.CUSTOMER_USER,
            }
            or self.status is not CustomerUserStatus.PENDING_ACTIVATION
            or self.security_revision != 1
            or self.optimistic_revision != 1
            or not self.normalized_login_email
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise IdentityAdministrationInputError("Customer account record is invalid")


@dataclass(frozen=True, slots=True)
class OrganizationView:
    """Return bounded Organization administration facts."""

    organization_id: str
    display_name: str
    status: OrganizationStatus
    policy_revision: int
    optimistic_revision: int


@dataclass(frozen=True, slots=True)
class OrganizationListQuery:
    """Read one deterministic Organization page as a Platform Administrator."""

    actor: AdministrationActor
    page_size: int = 50
    after_display_name: str | None = None
    after_organization_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.actor.principal_kind
            is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR
            or not 1 <= self.page_size <= 100
        ):
            raise IdentityAdministrationInputError(
                "Organization page query is invalid"
            )
        if (self.after_display_name is None) != (
            self.after_organization_id is None
        ):
            raise IdentityAdministrationInputError(
                "Organization page cursor is invalid"
            )
        if self.after_display_name is not None:
            _validate_display_name(self.after_display_name)
        if self.after_organization_id is not None:
            _validate_organization_id(self.after_organization_id)


@dataclass(frozen=True, slots=True)
class OrganizationPage:
    """Return a bounded Organization page and its next keyset cursor."""

    items: tuple[OrganizationView, ...]
    next_after_display_name: str | None
    next_after_organization_id: str | None


@dataclass(frozen=True, slots=True)
class CustomerUserView:
    """Return bounded customer-account facts without credentials or tokens."""

    user_id: str
    organization_id: str
    normalized_login_email: str
    display_name: str
    principal_kind: LoginPrincipalKind
    status: CustomerUserStatus
    security_revision: int
    optimistic_revision: int


@dataclass(frozen=True, slots=True)
class CustomerUserListQuery:
    """Read one tenant-bound customer-user page with an opaque identity cursor."""

    actor: AdministrationActor
    organization_id: str
    status: CustomerUserStatus | None = None
    page_size: int = 50
    after_user_id: str | None = None

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        if self.after_user_id is not None:
            _validate_uuid(self.after_user_id)
        if (
            self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            or self.actor.organization_id != self.organization_id
            or not 1 <= self.page_size <= 100
        ):
            raise IdentityAdministrationNotFoundError(
                "Customer accounts are not available"
            )


@dataclass(frozen=True, slots=True)
class CustomerUserPage:
    """Return a deterministic tenant-bound customer-user page."""

    items: tuple[CustomerUserView, ...]
    next_after_user_id: str | None


@dataclass(frozen=True, slots=True)
class CustomerUserDetailQuery:
    """Read one exact customer account inside the actor's Organization."""

    actor: AdministrationActor
    organization_id: str
    user_id: str

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        _validate_uuid(self.user_id)
        if (
            self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            or self.actor.organization_id != self.organization_id
        ):
            raise IdentityAdministrationNotFoundError(
                "Customer account is not available"
            )


@dataclass(frozen=True, slots=True)
class ProvisionedCustomerUserResult:
    """Disclose one activation token only after customer creation commits."""

    user: CustomerUserView
    activation_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActivateCustomerUserCommand:
    """Consume one activation token and establish the first password."""

    token: str = field(repr=False)
    password: str = field(repr=False)
    source_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CustomerCredentialMutation:
    """Bind token consumption to a password and account-state transition."""

    proof: RecoveryTokenProof
    password_hash: PasswordHash = field(repr=False)
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AdministrationSecurityEvent:
    """Describe a tenant-scoped event without customer values or secrets."""

    event_id: str
    deployment_id: str
    kind: AdministrationEventKind
    organization_id: str | None
    subject_user_id: str | None
    actor: AdministrationActor | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.event_id)
        if self.organization_id is not None:
            _validate_organization_id(self.organization_id)
        if self.subject_user_id is not None:
            _validate_uuid(self.subject_user_id)
        if (
            not self.deployment_id
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise IdentityAdministrationInputError("Administration event is invalid")


@dataclass(frozen=True, slots=True)
class AdministrationEventQuery:
    """Read one bounded newest-first page inside one Organization."""

    actor: AdministrationActor
    organization_id: str
    user_id: str | None = None
    page_size: int = 50
    before_occurred_at: datetime | None = None
    before_event_id: str | None = None

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        if self.user_id is not None:
            _validate_uuid(self.user_id)
        if (
            not 1 <= self.page_size <= 100
            or self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        ):
            raise IdentityAdministrationInputError("Event page size is invalid")
        if (self.before_occurred_at is None) != (self.before_event_id is None):
            raise IdentityAdministrationInputError("Event cursor is invalid")
        if self.before_event_id is not None:
            _validate_uuid(self.before_event_id)
        if self.actor.organization_id != self.organization_id:
            raise IdentityAdministrationNotFoundError("Security events are not available")


@dataclass(frozen=True, slots=True)
class AdministrationEventView:
    """Return one bounded event without a login identifier or secret."""

    event_id: str
    kind: AdministrationEventKind
    organization_id: str
    subject_user_id: str | None
    actor_kind: LoginPrincipalKind | None
    actor_id: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AdministrationEventPage:
    """Return deterministic newest-first administration events."""

    items: tuple[AdministrationEventView, ...]
    next_before_occurred_at: datetime | None
    next_before_event_id: str | None


@dataclass(frozen=True, slots=True)
class OrganizationSessionQuery:
    """List one bounded session page for one Organization and optional user."""

    actor: AdministrationActor
    organization_id: str
    user_id: str | None = None
    page_size: int = 50
    before_created_at: datetime | None = None
    before_session_id: str | None = None

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        if self.user_id is not None:
            _validate_uuid(self.user_id)
        if (
            not 1 <= self.page_size <= 100
            or self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        ):
            raise IdentityAdministrationInputError("Session page size is invalid")
        if (self.before_created_at is None) != (self.before_session_id is None):
            raise IdentityAdministrationInputError("Session cursor is invalid")
        if self.before_session_id is not None:
            _validate_uuid(self.before_session_id)
        if self.actor.organization_id != self.organization_id:
            raise IdentityAdministrationNotFoundError("Sessions are not available")


@dataclass(frozen=True, slots=True)
class OrganizationSessionView:
    """Return revocable session facts without handle or CSRF digests."""

    session_id: str
    user_id: str
    authentication_method: str
    state: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    optimistic_revision: int


@dataclass(frozen=True, slots=True)
class OrganizationSessionPage:
    """Return deterministic newest-first Organization sessions."""

    items: tuple[OrganizationSessionView, ...]
    next_before_created_at: datetime | None
    next_before_session_id: str | None


@dataclass(frozen=True, slots=True)
class RevokeOrganizationSessionCommand:
    """Revoke one exact session without revealing cross-Organization existence."""

    actor: AdministrationActor
    organization_id: str
    session_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        _validate_organization_id(self.organization_id)
        _validate_uuid(self.session_id)
        if (
            self.expected_revision <= 0
            or self.actor.principal_kind
            is not LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
        ):
            raise IdentityAdministrationInputError("Session revision is invalid")
        if self.actor.organization_id != self.organization_id:
            raise IdentityAdministrationNotFoundError("Session is not available")


def _validate_uuid(value: str) -> None:
    if _UUID_PATTERN.fullmatch(value) is None:
        raise IdentityAdministrationInputError("Identity is invalid")


def _validate_organization_id(value: str | None) -> None:
    if value is None or _ORGANIZATION_ID_PATTERN.fullmatch(value) is None:
        raise IdentityAdministrationInputError("Organization identity is invalid")


def _validate_display_name(value: str) -> None:
    if not value or value != value.strip() or len(value) > 200 or "\x00" in value:
        raise IdentityAdministrationInputError("Display name is invalid")


__all__ = [
    "ActivateCustomerUserCommand",
    "AdministrationActor",
    "AdministrationEventKind",
    "AdministrationEventPage",
    "AdministrationEventQuery",
    "AdministrationEventView",
    "AdministrationSecurityEvent",
    "CreateCustomerUserCommand",
    "CreateOrganizationCommand",
    "CustomerCredentialMutation",
    "CustomerUserDetailQuery",
    "CustomerUserListQuery",
    "CustomerUserMutationCommand",
    "CustomerUserPage",
    "CustomerUserRecord",
    "CustomerUserStatus",
    "CustomerUserView",
    "IdentityAdministrationConflictError",
    "IdentityAdministrationError",
    "IdentityAdministrationInputError",
    "IdentityAdministrationNotFoundError",
    "IdentityAdministrationStorageError",
    "IdentityAdministrationUnavailableError",
    "OrganizationListQuery",
    "OrganizationMutationCommand",
    "OrganizationPage",
    "OrganizationSessionPage",
    "OrganizationSessionQuery",
    "OrganizationSessionView",
    "OrganizationStatus",
    "OrganizationView",
    "ProvisionedCustomerUserResult",
    "RenameOrganizationCommand",
    "ReplaceOrganizationAdministratorCommand",
    "ReviseOrganizationPolicyCommand",
    "RevokeOrganizationSessionCommand",
]
