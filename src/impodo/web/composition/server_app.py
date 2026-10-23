"""Declare the fail-closed shared-server application composition boundary."""

from __future__ import annotations

from fastapi import FastAPI


class ServerCompositionError(RuntimeError):
    """Report that required shared-server adapters are not yet assembled."""


def create_server_app() -> FastAPI:
    """Refuse startup until server identity and persistence adapters exist."""

    raise ServerCompositionError(
        "The shared-server composition is not available yet. "
        "PostgreSQL control-plane, server identity, session, artifact, and "
        "worker adapters must be configured; local fallbacks are forbidden."
    )
