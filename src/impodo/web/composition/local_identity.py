"""Resolve the implicit single-operator identity for local Impodo."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from impodo.application.shared.identity import ResolvedRequestIdentity
from impodo.domain.shared.access import LOCAL_ACTOR, Actor
from impodo.domain.shared.tenancy import (
    AuthenticatedPrincipal,
    AuthenticationMethod,
    AuthorizationReceipt,
    OrganizationId,
    PrincipalKind,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)

LOCAL_ORGANIZATION_ID = OrganizationId("impodo-local")


class LocalRequestIdentityResolver:
    """Return the fixed local Organization and trusted local operator."""

    def __init__(
        self,
        actor: Actor = LOCAL_ACTOR,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._actor = actor
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(
        self,
        session: Mapping[str, object],
    ) -> ResolvedRequestIdentity:
        """Resolve the current local request without adding login or role UI."""

        if session.get("authenticated") is not True:
            raise PermissionError("Local launch session is not authenticated")
        now = self._clock()
        principal = AuthenticatedPrincipal(
            principal_id=self._actor.identity.subject_id,
            actor_identity=self._actor.identity,
            kind=PrincipalKind.LOCAL,
            authentication_method=AuthenticationMethod.LOCAL_LAUNCH,
            authenticated_at=now,
            organization_id=LOCAL_ORGANIZATION_ID,
            session_id="local-launch-session",
        )
        scope = RequestOrganizationScope.for_principal(principal)
        authorization = AuthorizationReceipt(
            receipt_id="local-authorization",
            principal_id=principal.principal_id,
            actor_identity=principal.actor_identity,
            organization_id=LOCAL_ORGANIZATION_ID,
            capabilities=self._actor.capabilities,
            project_visibility=ProjectVisibilityScope(
                organization_id=LOCAL_ORGANIZATION_ID,
                organization_wide=True,
            ),
            role_revision_ids=("local-operator",),
            grant_revision_ids=("local-organization-wide",),
            policy_revision="local-trust-v1",
            issued_at=now,
        )
        return ResolvedRequestIdentity(
            principal=principal,
            organization_scope=scope,
            authorization=authorization,
            actor=self._actor,
        )
