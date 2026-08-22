"""Define cross-stage actor identity and command authorization contracts.

Local Impodo uses one trusted operator. A later hosted adapter will construct
the same :class:`Actor` from a verified corporate identity token and resolve
project membership before application services execute a command.

Application services call ``AuthorizationPolicy.require`` before reading or
mutating governed evidence. Repositories retain the verified stable
``ActorIdentity`` in audit records; display names alone are not identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Capability(StrEnum):
    """Fine-grained permissions used by application-service commands."""

    PROJECT_CREATE = "project.create"
    PROJECT_EDIT = "project.edit"
    PROJECT_REGISTER = "project.register"
    PROJECT_VIEW = "project.view"
    RECIPE_CREATE = "recipe.create"
    RECIPE_DELETE = "recipe.delete"
    RECIPE_VIEW = "recipe.view"
    RECIPE_APPLY = "recipe.apply"
    RECIPE_PUBLISH = "recipe.publish"
    DATA_VERSION_CREATE = "data_version.create"
    DATA_VERSION_EDIT = "data_version.edit"
    MIGRATION_RUN_CREATE = "migration_run.create"
    MIGRATION_RUN_EDIT = "migration_run.edit"
    MIGRATION_WORKSPACE_CREATE = "migration_workspace.create"
    MIGRATION_WORKSPACE_EDIT = "migration_workspace.edit"
    RECIPE_QUALIFY = "recipe.qualify"
    CUTOVER_SELECT = "cutover.select"
    LOCAL_STACK_INSPECT = "local_stack.inspect"
    LOCAL_STACK_START = "local_stack.start"
    LOCAL_STACK_STOP = "local_stack.stop"
    SOURCE_INSPECT = "source.inspect"
    SOURCE_CONFIGURE = "source.configure"
    SOURCE_SELECT = "source.select"
    SOURCE_CAPTURE = "source.capture"
    PROTECTED_EVIDENCE_READ = "protected_evidence.read"
    PROTECTED_EVIDENCE_MANAGE = "protected_evidence.manage"
    SCHEMA_DISCOVER = "schema.discover"
    SCHEMA_GOVERN = "schema.govern"
    MAPPING_EDIT = "mapping.edit"
    MAPPING_SUBMIT = "mapping.submit"
    COVERAGE_SCOPE = "coverage.scope"
    RESOLUTION_DECIDE = "resolution.decide"
    RESOLUTION_APPROVE = "resolution.approve"
    NORMALIZATION_DECIDE = "normalization.decide"
    NORMALIZATION_APPROVE = "normalization.approve"
    PREFLIGHT_RUN = "preflight.run"
    EXPORT_PLAN_APPROVE = "export_plan.approve"
    EXPORT_PLAN_EXECUTE = "export_plan.execute"
    AUDIT_VIEW = "audit.view"
    PROJECT_ADMIN = "project.admin"


ALL_CAPABILITIES = frozenset(Capability)


class AuthorizationError(PermissionError):
    """Raised when a verified actor lacks a required capability."""


@dataclass(frozen=True, slots=True, order=True)
class ActorIdentity:
    """Stable audit identity independent of display-name changes."""

    issuer: str
    subject_id: str
    display_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issuer",
            _required_text(self.issuer, "issuer", maximum=500),
        )
        object.__setattr__(
            self,
            "subject_id",
            _required_text(self.subject_id, "subject_id", maximum=500),
        )
        object.__setattr__(
            self,
            "display_name",
            _required_text(self.display_name, "display_name", maximum=200),
        )


@dataclass(frozen=True, slots=True)
class Actor:
    """Authenticated principal plus the capabilities resolved for a request."""

    identity: ActorIdentity
    capabilities: frozenset[Capability]
    email: str = ""

    def __post_init__(self) -> None:
        normalized = frozenset(Capability(item) for item in self.capabilities)
        object.__setattr__(self, "capabilities", normalized)
        clean_email = self.email.strip()
        if len(clean_email) > 320:
            raise ValueError("email is too long")
        object.__setattr__(self, "email", clean_email)

    def has(self, capability: Capability) -> bool:
        """Return whether this actor may exercise ``capability``."""

        return (
            capability in self.capabilities
            or Capability.PROJECT_ADMIN in self.capabilities
        )


class AuthorizationPolicy(Protocol):
    """Authorization port implemented by local and hosted deployments."""

    def require(
        self,
        actor: Actor,
        capability: Capability,
        *,
        project_id: str | None = None,
    ) -> None:
        """Allow the command or raise before its service accesses project state."""
        ...


class CapabilityAuthorizationPolicy:
    """Authorize from capabilities already resolved for the actor."""

    def require(
        self,
        actor: Actor,
        capability: Capability,
        *,
        project_id: str | None = None,
    ) -> None:
        """Require an already-resolved capability, honoring project-admin override."""

        if actor.has(capability):
            return
        scope = f" for project {project_id}" if project_id else ""
        raise AuthorizationError(
            f"{actor.identity.display_name} lacks {capability.value}{scope}"
        )


def _required_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


LOCAL_ACTOR = Actor(
    identity=ActorIdentity(
        issuer="urn:impodo:local",
        subject_id="local-operator",
        display_name="Local Impodo operator",
    ),
    capabilities=ALL_CAPABILITIES,
)
