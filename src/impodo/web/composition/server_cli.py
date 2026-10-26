"""Expose fail-closed operational commands for the shared-server image.

The Linux image has one entry point for its web, worker, migration, and
administrative process roles. Each role remains unavailable until its
deployment-owned adapters and lifecycle contract are implemented. The command
never falls back to the local browser launcher or local filesystem state.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from impodo.web.composition.server_app import (
    ServerCompositionError,
    create_server_app,
)

CONFIGURATION_ERROR_EXIT = 78

_PLANNED_COMMANDS = {
    "worker": "durable server job coordination is not implemented yet",
    "migrate": "the PostgreSQL schema migration runner is not implemented yet",
    "admin": "the server administration command set is not implemented yet",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit process-role selector for the one server image."""

    parser = argparse.ArgumentParser(
        prog="impodo-server",
        description="Run one explicit Impodo shared-server process role.",
    )
    parser.add_argument(
        "command",
        choices=("web", "worker", "migrate", "admin"),
        help="process role to start from the shared production image",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one server role and refuse every incomplete composition."""

    command = build_parser().parse_args(argv).command
    if command == "web":
        try:
            create_server_app()
        except ServerCompositionError as error:
            print(f"Impodo server web startup refused: {error}", file=sys.stderr)
            return CONFIGURATION_ERROR_EXIT
        raise ServerCompositionError(
            "The server application graph exists, but no production ASGI "
            "process runner has been connected."
        )

    print(
        f"Impodo server {command} startup refused: {_PLANNED_COMMANDS[command]}.",
        file=sys.stderr,
    )
    return CONFIGURATION_ERROR_EXIT


__all__ = ["CONFIGURATION_ERROR_EXIT", "build_parser", "main"]
