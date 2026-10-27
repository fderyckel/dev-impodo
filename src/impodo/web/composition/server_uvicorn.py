"""Bind the shared-server ASGI process to one production Uvicorn worker."""

from __future__ import annotations

from collections.abc import Callable
from types import FrameType

import uvicorn
from starlette.types import ASGIApp

from impodo.web.composition.server_config import ServerSettings


class DrainingUvicornServer(uvicorn.Server):
    """Withdraw Impodo admission as soon as Uvicorn receives a stop signal."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        begin_draining: Callable[[], object],
    ) -> None:
        super().__init__(config)
        self._begin_draining = begin_draining

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Close application admission before Uvicorn starts transport drain."""

        self._begin_draining()
        super().handle_exit(sig, frame)


def create_production_uvicorn_server(
    app: ASGIApp,
    settings: ServerSettings,
    *,
    begin_draining: Callable[[], object],
) -> DrainingUvicornServer:
    """Create one bounded worker behind only the configured trusted proxies."""

    config = uvicorn.Config(
        app,
        host=str(settings.web_bind_host),
        port=settings.web_bind_port,
        workers=1,
        lifespan="on",
        log_level=settings.log_level.value.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=[
            str(network) for network in settings.trusted_proxy_networks
        ],
        server_header=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=settings.drain_timeout_seconds,
    )
    return DrainingUvicornServer(config, begin_draining=begin_draining)


__all__ = [
    "DrainingUvicornServer",
    "create_production_uvicorn_server",
]
