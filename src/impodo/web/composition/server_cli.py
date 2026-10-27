"""Expose fail-closed operational commands for the shared-server image.

The Linux image has one entry point for its web, worker, migration, and
administrative process roles. The web role serves only lifecycle routes until
the shared application graph is complete. The migration role owns its narrow
privileged configuration, while incomplete roles still fail closed. The
command never falls back to the local browser launcher or local filesystem
state.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from impodo.web.composition.server_config import (
    ServerConfigurationError,
    load_server_migration_settings,
    load_server_settings,
)
from impodo.web.composition.server_linux_migration import (
    ServerMigrationExecutionError,
    ServerMigrationIncompatibleError,
    ServerMigrationUnavailableError,
    run_file_secret_linux_postgresql_migrations,
)
from impodo.web.composition.server_linux_web import (
    run_file_secret_linux_server_web,
)

SOFTWARE_ERROR_EXIT = 70
TEMPORARY_FAILURE_EXIT = 75
CONFIGURATION_ERROR_EXIT = 78

_PLANNED_COMMANDS = {
    "worker": "durable server job coordination is not implemented yet",
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
    if command == "migrate":
        try:
            settings = load_server_migration_settings()
            result = run_file_secret_linux_postgresql_migrations(settings)
        except ServerConfigurationError as error:
            print(
                f"Impodo server migrate startup refused: {error}",
                file=sys.stderr,
            )
            return CONFIGURATION_ERROR_EXIT
        except ServerMigrationUnavailableError:
            print(
                "Impodo server migrate failed: PostgreSQL is temporarily unavailable.",
                file=sys.stderr,
            )
            return TEMPORARY_FAILURE_EXIT
        except ServerMigrationIncompatibleError:
            print(
                "Impodo server migrate refused: PostgreSQL migration history "
                "is incompatible.",
                file=sys.stderr,
            )
            return CONFIGURATION_ERROR_EXIT
        except ServerMigrationExecutionError:
            print(
                "Impodo server migrate failed: PostgreSQL migration execution "
                "did not complete.",
                file=sys.stderr,
            )
            return SOFTWARE_ERROR_EXIT

        applied = (
            ",".join(str(version) for version in result.applied_versions) or "none"
        )
        print(
            "Impodo server migrate completed: "
            f"schema version {result.current_version}; applied {applied}."
        )
        return 0

    if command == "web":
        try:
            settings = load_server_settings()
            run_file_secret_linux_server_web(settings)
        except ServerConfigurationError as error:
            print(f"Impodo server web startup refused: {error}", file=sys.stderr)
            return CONFIGURATION_ERROR_EXIT
        return 0

    try:
        load_server_settings()
    except ServerConfigurationError as error:
        print(
            f"Impodo server {command} startup refused: {error}",
            file=sys.stderr,
        )
        return CONFIGURATION_ERROR_EXIT

    print(
        f"Impodo server {command} startup refused: {_PLANNED_COMMANDS[command]}.",
        file=sys.stderr,
    )
    return CONFIGURATION_ERROR_EXIT


__all__ = [
    "CONFIGURATION_ERROR_EXIT",
    "SOFTWARE_ERROR_EXIT",
    "TEMPORARY_FAILURE_EXIT",
    "build_parser",
    "main",
]
