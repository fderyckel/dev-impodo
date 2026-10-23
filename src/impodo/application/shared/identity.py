"""Describe the verified identity and authorization bound to one request."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.shared.access import Actor
from impodo.domain.shared.tenancy import (
    AuthenticatedPrincipal,
    AuthorizationReceipt,
    RequestOrganizationScope,
)


@dataclass(frozen=True, slots=True)
class ResolvedRequestIdentity:
    """Carry one internally consistent request identity into application code."""

    principal: AuthenticatedPrincipal
    organization_scope: RequestOrganizationScope
    authorization: AuthorizationReceipt
    actor: Actor

    def __post_init__(self) -> None:
        if self.organization_scope.principal_id != self.principal.principal_id:
            raise ValueError("request scope principal does not match authentication")
        if self.authorization.principal_id != self.principal.principal_id:
            raise ValueError("authorization principal does not match authentication")
        if (
            self.organization_scope.organization_id
            != self.authorization.organization_id
        ):
            raise ValueError("request scope and authorization Organization differ")
        if self.actor.identity != self.principal.actor_identity:
            raise ValueError("actor identity does not match authentication")
        if self.authorization.actor_identity != self.principal.actor_identity:
            raise ValueError("authorization identity does not match authentication")
        if self.actor.capabilities != self.authorization.capabilities:
            raise ValueError("actor capabilities do not match authorization")
