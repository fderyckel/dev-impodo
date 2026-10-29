"""Assemble and run the Linux shared-server web process."""

from __future__ import annotations

from impodo.web.composition.server_administration import (
    create_server_administration_router,
)
from impodo.web.composition.server_config import ServerSettings
from impodo.web.composition.server_linux_administration import (
    create_file_secret_server_administration_dependencies,
)
from impodo.web.composition.server_linux_runtime import (
    create_file_secret_linux_server_runtime,
)
from impodo.web.composition.server_runtime_app import (
    create_server_runtime_control_app,
)
from impodo.web.composition.server_uvicorn import (
    create_production_uvicorn_server,
)


def run_file_secret_linux_server_web(settings: ServerSettings) -> None:
    """Run health, login, and bounded administration routes."""

    runtime = create_file_secret_linux_server_runtime(settings)
    app = create_server_runtime_control_app(runtime, public_url=settings.public_url)
    dependencies = create_file_secret_server_administration_dependencies(settings)
    app.include_router(create_server_administration_router(dependencies))
    server = create_production_uvicorn_server(
        app,
        settings,
        begin_draining=runtime.begin_draining,
    )
    server.run()


__all__ = ["run_file_secret_linux_server_web"]
