"""Windows-friendly launcher for the local Impodo browser application."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import sys
from urllib.parse import quote
import webbrowser

import uvicorn

from impodo.adapters.protected_evidence.project_security import (
    DEVELOPMENT_MODE_ENV,
    PROJECT_ROOT_ENV,
    ProjectRootSecurityError,
    development_mode_enabled,
    prepare_project_root,
)
from .app import create_local_app


def default_project_root(*, development_mode: bool | None = None) -> Path:
    active_development_mode = (
        development_mode_enabled()
        if development_mode is None
        else development_mode
    )
    configured = os.environ.get(PROJECT_ROOT_ENV)
    if configured:
        if os.name == "nt" and not active_development_mode:
            raise ProjectRootSecurityError(
                f"{PROJECT_ROOT_ENV} is available only when "
                f"{DEVELOPMENT_MODE_ENV}=1. Normal internal use keeps data in "
                "%LOCALAPPDATA%\\Impodo\\projects."
            )
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Impodo" / "projects"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Impodo" / "projects"
    return Path.cwd() / "var" / "projects"


def main() -> int:
    """Bind loopback first, open the authenticated URL, and serve until quit."""

    development_mode = development_mode_enabled()
    try:
        project_root = default_project_root(development_mode=development_mode)
        root_security = prepare_project_root(
            project_root,
            development_mode=development_mode,
        )
    except ProjectRootSecurityError as error:
        print(f"Impodo refused the project-data root: {error}", file=sys.stderr)
        return 2

    if root_security.development_mode:
        print(
            "WARNING: Impodo development mode does not enforce the internal-data "
            "storage policy. Use only synthetic or disposable data.",
            file=sys.stderr,
        )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    expected_host = f"127.0.0.1:{port}"
    launch_token = secrets.token_urlsafe(32)
    app = create_local_app(
        root_security.root,
        expected_host=expected_host,
        launch_token=launch_token,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=20,
        timeout_keep_alive=5,
    )
    server = uvicorn.Server(config)
    app.state.server = server
    url = (
        f"http://{expected_host}/launch?"
        f"token={quote(launch_token, safe='')}"
    )
    webbrowser.open(url, new=1)
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        return 0
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
