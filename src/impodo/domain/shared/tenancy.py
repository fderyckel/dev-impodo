"""Portable tenancy, authentication, and authorization evidence contracts.

These values describe facts verified by a deployment adapter. They do not
authenticate a request, query PostgreSQL, or make the local composition
multi-user. Application services can accept them without depending on either
the local launch session or a future server identity implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from impodo.domain.shared.access import ActorIdentity, Capability


class PrincipalKind(StrEnum):
    """Supported principal ownership models."""

    LOCAL = "LOCAL"
    CUSTOMER = "CUSTOMER"
    PLATFORM = "PLATFORM"


class AuthenticationMethod(StrEnum):
    """How a deployment verified a principal."""

    LOCAL_LAUNCH = "LOCAL_LAUNCH"
    IMPODO_PASSWORD = "IMPODO_PASSWORD"
    OIDC = "OIDC"


@dataclass(frozen=True, slots=True, order=True)
class OrganizationId:
    """Stable server tenancy identity, independent of a Company label."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _required_text(self.value, "organization_id", maximum=200),
        )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """One verified account identity before authorization is resolved."""

    principal_id: str
    actor_identity: ActorIdentity
    kind: PrincipalKind
    authentication_method: AuthenticationMethod
    authenticated_at: datetime
    organization_id: OrganizationId | None
    session_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", PrincipalKind(self.kind))
        object.__setattr__(
            self,
            "authentication_method",
            AuthenticationMethod(self.authentication_method),
        )
        object.__setattr__(
            self,
            "principal_id",
            _required_text(self.principal_id, "principal_id", maximum=200),
        )
        object.__setattr__(
            self,
            "session_id",
            _required_text(self.session_id, "session_id", maximum=200),
        )
        _aware(self.authenticated_at, "authenticated_at")
        if self.kind in {PrincipalKind.LOCAL, PrincipalKind.CUSTOMER}:
            if self.organization_id is None:
                raise ValueError(
                    f"{self.kind.value.lower()} principal must belong to one "
                    "Organization"
                )
        elif self.organization_id is not None:
            raise ValueError(
                "platform principal membership must not bind a customer Organization"
            )


@dataclass(frozen=True, slots=True)
class RequestOrganizationScope:
    """Fixed Organization selected before a request enters application code."""

    organization_id: OrganizationId
    principal_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal_id",
            _required_text(self.principal_id, "principal_id", maximum=200),
        )

    @classmethod
    def for_principal(
        cls,
        principal: AuthenticatedPrincipal,
        *,
        organization_id: OrganizationId | None = None,
    ) -> "RequestOrganizationScope":
        """Bind customer/local membership or an explicit platform scope."""

        selected = organization_id or principal.organization_id
        if selected is None:
            raise ValueError("platform requests require an explicit Organization scope")
        if (
            principal.organization_id is not None
            and selected != principal.organization_id
        ):
            raise ValueError("principal cannot switch to another Organization")
        return cls(organization_id=selected, principal_id=principal.principal_id)


@dataclass(frozen=True, slots=True)
class ProjectVisibilityScope:
    """Projects a verified principal may discover within one Organization."""

    organization_id: OrganizationId
    project_ids: frozenset[str] = frozenset()
    organization_wide: bool = False

    def __post_init__(self) -> None:
        normalized = frozenset(
            _required_text(project_id, "project_id", maximum=200)
            for project_id in self.project_ids
        )
        object.__setattr__(self, "project_ids", normalized)

    def allows(self, project_id: str) -> bool:
        """Return whether a Project may be discovered in this fixed scope."""

        candidate = _required_text(project_id, "project_id", maximum=200)
        return self.organization_wide or candidate in self.project_ids


@dataclass(frozen=True, slots=True)
class AuthorizationReceipt:
    """Immutable effective authorization resolved for one bounded request."""

    receipt_id: str
    principal_id: str
    actor_identity: ActorIdentity
    organization_id: OrganizationId
    capabilities: frozenset[Capability]
    project_visibility: ProjectVisibilityScope
    role_revision_ids: tuple[str, ...]
    grant_revision_ids: tuple[str, ...]
    policy_revision: str
    issued_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("receipt_id", "principal_id", "policy_revision"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name, maximum=200),
            )
        if self.project_visibility.organization_id != self.organization_id:
            raise ValueError(
                "authorization and Project visibility must use one Organization"
            )
        object.__setattr__(
            self,
            "capabilities",
            frozenset(Capability(item) for item in self.capabilities),
        )
        object.__setattr__(
            self,
            "role_revision_ids",
            _identities(self.role_revision_ids, "role_revision_id"),
        )
        object.__setattr__(
            self,
            "grant_revision_ids",
            _identities(self.grant_revision_ids, "grant_revision_id"),
        )
        _aware(self.issued_at, "issued_at")
        if self.expires_at is not None:
            _aware(self.expires_at, "expires_at")
            if self.expires_at <= self.issued_at:
                raise ValueError("expires_at must be later than issued_at")

    def is_valid_at(self, at: datetime) -> bool:
        """Return whether this frozen authorization has not expired."""

        _aware(at, "at")
        return self.expires_at is None or at < self.expires_at

    def authorizes(
        self,
        capability: Capability,
        *,
        at: datetime,
        project_id: str | None = None,
    ) -> bool:
        """Check capability, expiry, and optional Project visibility together."""

        if not self.is_valid_at(at):
            return False
        if (
            capability not in self.capabilities
            and Capability.PROJECT_ADMIN not in self.capabilities
        ):
            return False
        return project_id is None or self.project_visibility.allows(project_id)


def _identities(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    normalized = tuple(
        sorted({_required_text(value, name, maximum=200) for value in values})
    )
    return normalized


def _required_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


def _aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
