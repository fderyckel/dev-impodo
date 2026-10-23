"""Resolve and cache identity evidence for one browser request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from fastapi import Request

from impodo.application.shared.identity import ResolvedRequestIdentity


class RequestIdentityResolver(Protocol):
    """Resolve authentication and effective authorization for one request."""

    def resolve(
        self,
        session: Mapping[str, object],
    ) -> ResolvedRequestIdentity:
        """Return verified request identity or fail before application access."""
        ...


def request_identity(
    request: Request,
    resolver: RequestIdentityResolver,
) -> ResolvedRequestIdentity:
    """Return one resolved identity instance for the lifetime of ``request``."""

    existing = getattr(request.state, "impodo_request_identity", None)
    if existing is not None:
        return existing
    resolved = resolver.resolve(request.session)
    request.state.impodo_request_identity = resolved
    return resolved
