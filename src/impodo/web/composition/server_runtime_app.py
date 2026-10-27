"""Connect server runtime coordination to an ASGI process lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from impodo.web.composition.server_active_work import (
    ServerWorkAdmissionClosedError,
)
from impodo.web.composition.server_control_app import create_server_control_app
from impodo.web.composition.server_runtime import ServerRuntimeCoordinator

_HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})
_UNAVAILABLE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


class ServerActiveWorkMiddleware:
    """Hold an active-work lease through each complete application response."""

    def __init__(self, app: ASGIApp, *, runtime: ServerRuntimeCoordinator) -> None:
        self._app = app
        self._runtime = runtime

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"} or (
            scope_type == "http" and scope.get("path") in _HEALTH_PATHS
        ):
            await self._app(scope, receive, send)
            return
        try:
            lease = self._runtime.begin_work()
        except ServerWorkAdmissionClosedError:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            response = JSONResponse(
                {"detail": "Service temporarily unavailable"},
                status_code=503,
                headers=_UNAVAILABLE_HEADERS,
            )
            await response(scope, receive, send)
            return
        try:
            await self._app(scope, receive, send)
        finally:
            lease.complete()


def create_server_runtime_control_app(
    runtime: ServerRuntimeCoordinator,
) -> FastAPI:
    """Start and drain the runtime outside the ASGI event-loop thread."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(runtime.startup)
        try:
            yield
        finally:
            await run_in_threadpool(runtime.begin_draining)
            await run_in_threadpool(runtime.complete_draining)

    app = create_server_control_app(runtime.snapshot, lifespan=lifespan)
    app.add_middleware(ServerActiveWorkMiddleware, runtime=runtime)
    return app


__all__ = [
    "ServerActiveWorkMiddleware",
    "create_server_runtime_control_app",
]
