"""Windows-friendly launcher for the local Impodo browser application."""

from __future__ import annotations

import os
from pathlib import Path
from multiprocessing import freeze_support
import secrets
import socket
import sys

from impodo.adapters.protected_evidence.project_security import (
    DEVELOPMENT_MODE_ENV,
    PROJECT_ROOT_ENV,
    ProjectRootSecurityError,
    development_mode_enabled,
    prepare_project_root,
)
from .diagnostics import diagnostic_directory
from .server_supervisor import (
    ServerChildSettings,
    record_launcher_event,
    supervise_server,
)


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
    """Bind loopback, then supervise a restartable web-server child."""

    freeze_support()
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

    diagnostics_root: Path | None = None
    try:
        diagnostics_root = diagnostic_directory(root_security.root)
        prepare_project_root(
            diagnostics_root,
            development_mode=development_mode,
        )
    except (OSError, ProjectRootSecurityError):
        diagnostics_root = None
        print(
            "WARNING: Impodo could not secure or open its local diagnostic log.",
            file=sys.stderr,
        )
    record_launcher_event(
        diagnostics_root,
        "launcher_starting",
        development_mode=root_security.development_mode,
    )

    listener: socket.socket | None = None
    exit_code = 1
    stop_reason = "startup_failed"
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        expected_host = f"127.0.0.1:{port}"
        record_launcher_event(diagnostics_root, "server_bound", port=port)
        launch_token = secrets.token_urlsafe(32)
        session_secret = secrets.token_urlsafe(48)
        result = supervise_server(
            listener,
            ServerChildSettings(
                project_root=root_security.root,
                expected_host=expected_host,
                launch_token=launch_token,
                session_secret=session_secret,
                diagnostics_root=diagnostics_root,
                development_mode=root_security.development_mode,
            ),
        )
        exit_code = result.exit_code
        stop_reason = result.reason
    except BaseException as error:
        stop_reason = "unhandled_exception"
        record_launcher_event(
            diagnostics_root,
            "launcher_failed",
            reason=stop_reason,
            exit_code=exit_code,
            exception_class=type(error).__name__,
        )
        raise
    finally:
        if listener is not None:
            listener.close()
        record_launcher_event(
            diagnostics_root,
            "launcher_stopped",
            reason=stop_reason,
            exit_code=exit_code,
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
