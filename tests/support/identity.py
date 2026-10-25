"""Test helpers for resolving the implicit local request identity."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.application.shared.identity import ResolvedRequestIdentity
from impodo.domain.shared.access import LOCAL_ACTOR, Actor
from impodo.web.composition.local_identity import LocalRequestIdentityResolver
from impodo.web.context import WebContext


def resolve_local_actor(context: WebContext) -> Actor:
    """Resolve the actor through the same authenticated local-session seam."""

    return context.identity_resolver.resolve({"authenticated": True}).actor


class MutableLocalRequestIdentityResolver:
    """Let a browser test change identity without replacing a captured resolver."""

    def __init__(self, actor: Actor = LOCAL_ACTOR) -> None:
        self.actor = actor

    def resolve(
        self,
        session: Mapping[str, object],
    ) -> ResolvedRequestIdentity:
        return LocalRequestIdentityResolver(self.actor).resolve(session)
