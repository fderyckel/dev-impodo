"""Expose only privacy-safe health routes for an incomplete server runtime."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from impodo.web.composition.server_lifecycle import (
    ServerLifecycle,
    liveness_payload,
)

ServerLifecycleReader = Callable[[], ServerLifecycle]

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def create_server_control_app(
    lifecycle_reader: ServerLifecycleReader,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Build an ASGI app that cannot expose migration or administration work."""

    app = FastAPI(
        title="Impodo server control plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live", include_in_schema=False)
    async def health_live() -> JSONResponse:
        return JSONResponse(
            liveness_payload(),
            status_code=200,
            headers=_NO_STORE_HEADERS,
        )

    @app.get("/health/ready", include_in_schema=False)
    async def health_ready() -> JSONResponse:
        lifecycle = lifecycle_reader()
        return JSONResponse(
            lifecycle.readiness_payload(),
            status_code=200 if lifecycle.is_ready else 503,
            headers=_NO_STORE_HEADERS,
        )

    return app


__all__ = ["ServerLifecycleReader", "create_server_control_app"]
