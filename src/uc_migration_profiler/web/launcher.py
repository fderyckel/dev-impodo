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

from .app import create_app


def default_project_root() -> Path:
    configured = os.environ.get("IMPODO_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Impodo" / "projects"
    return Path.cwd() / "var" / "projects"


def main() -> int:
    """Bind loopback first, open the authenticated URL, and serve until quit."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    expected_host = f"127.0.0.1:{port}"
    launch_token = secrets.token_urlsafe(32)
    app = create_app(
        default_project_root(),
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
