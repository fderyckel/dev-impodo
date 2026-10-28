"""Prove the private migration role uses narrow settings and safe outcomes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
import unittest
from typing import LiteralString
from unittest.mock import patch

from impodo.adapters.postgresql.control_schema import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
)
from impodo.adapters.postgresql.migrations import (
    POSTGRESQL_MIGRATIONS,
    PostgreSQLMigrationResult,
)
from impodo.web.composition.server_cli import (
    CONFIGURATION_ERROR_EXIT,
    SOFTWARE_ERROR_EXIT,
    TEMPORARY_FAILURE_EXIT,
    main,
)
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


def _migration_environment() -> dict[str, str]:
    return {
        "IMPODO_DEPLOYMENT_ID": "shared-primary",
        "IMPODO_POSTGRES_MIGRATION_DSN_FILE": ("/run/secrets/postgres-migration-dsn"),
        "IMPODO_SECRET_PROVIDER": "files",
        "IMPODO_SECRET_ROOT": "/run/secrets",
    }


def _runtime_environment() -> dict[str, str]:
    return {
        "IMPODO_DEPLOYMENT_ID": "shared-primary",
        "IMPODO_PUBLIC_URL": "https://impodo.example.test",
        "IMPODO_TRUSTED_PROXY_CIDRS": "172.20.0.0/24",
        "IMPODO_WEB_BIND_HOST": "0.0.0.0",
        "IMPODO_WEB_BIND_PORT": "8000",
        "IMPODO_DRAIN_TIMEOUT_SECONDS": "30",
        "IMPODO_POSTGRES_DSN_FILE": "/run/secrets/postgres-runtime-dsn",
        "IMPODO_ARTIFACT_ROOT": "/var/lib/impodo/artifacts",
        "IMPODO_RUNTIME_ROOT": "/var/lib/impodo/runtime",
        "IMPODO_TEMP_ROOT": "/var/tmp/impodo",
        "IMPODO_SECRET_PROVIDER": "files",
        "IMPODO_SECRET_ROOT": "/run/secrets",
        "IMPODO_WORKER_CAPACITY": "8",
        "IMPODO_ODOO_EGRESS_MODE": "disabled",
    }


class _FileOperations:
    def __init__(self) -> None:
        self.closed: list[int] = []
        self.opened_child: str | None = None

    def open_directory(self, path: PurePosixPath) -> int:
        if path != PurePosixPath("/run/secrets"):
            raise AssertionError(path)
        return 10

    def open_child(self, directory_fd: int, name: str) -> int:
        if directory_fd != 10:
            raise AssertionError(directory_fd)
        self.opened_child = name
        return 11

    def metadata(self, descriptor: int) -> SimpleNamespace:
        if descriptor == 10:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_uid=0,
                st_gid=10001,
                st_nlink=1,
                st_size=0,
            )
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o640,
            st_uid=0,
            st_gid=10001,
            st_nlink=1,
            st_size=len(b"private-migration-dsn"),
        )

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        if descriptor != 11 or maximum_bytes <= len(b"private-migration-dsn"):
            raise AssertionError((descriptor, maximum_bytes))
        return b"private-migration-dsn"

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)


class _MigrationSession:
    def __init__(self) -> None:
        self.responses = [
            (),
            (
                (
                    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
                    POSTGRESQL_CONTROL_SCHEMA_VERSION,
                ),
            ),
        ]
        self.committed = False
        self.closed = False

    def execute(
        self,
        _query: LiteralString,
        _parameters: tuple[object, ...] = (),
    ) -> None:
        return None

    def fetch_rows(
        self,
        _query: LiteralString,
        _parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        return self.responses.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        raise AssertionError("successful migration must not roll back")

    def close(self) -> None:
        self.closed = True


class ServerMigrationConfigurationTests(unittest.TestCase):
    def test_migration_settings_accept_only_the_privileged_role_boundary(self) -> None:
        settings = load_server_migration_settings(_migration_environment())

        self.assertEqual(settings.deployment_id, "shared-primary")
        self.assertEqual(
            settings.postgres_migration_dsn_file,
            PurePosixPath("/run/secrets/postgres-migration-dsn"),
        )

        runtime_environment = _runtime_environment()
        runtime_environment["IMPODO_POSTGRES_MIGRATION_DSN_FILE"] = (
            "/run/secrets/postgres-migration-dsn"
        )
        with self.assertRaisesRegex(ServerConfigurationError, "Unknown"):
            load_server_settings(runtime_environment)

        with self.assertRaisesRegex(ServerConfigurationError, "Unknown"):
            load_server_migration_settings(_runtime_environment())

    def test_migration_secret_must_be_a_direct_child_of_its_root(self) -> None:
        environment = _migration_environment()
        environment["IMPODO_POSTGRES_MIGRATION_DSN_FILE"] = (
            "/run/secrets/database/postgres-migration-dsn"
        )

        with self.assertRaisesRegex(ServerConfigurationError, "direct children"):
            load_server_migration_settings(environment)


class ServerMigrationCommandTests(unittest.TestCase):
    def test_success_reports_only_versions(self) -> None:
        result = PostgreSQLMigrationResult(applied_versions=(1,), current_version=1)
        output = StringIO()
        with (
            patch.dict("os.environ", _migration_environment(), clear=True),
            patch(
                "impodo.web.composition.server_cli."
                "run_file_secret_linux_postgresql_migrations",
                return_value=result,
            ) as migrate,
            redirect_stdout(output),
        ):
            exit_code = main(("migrate",))

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue().strip(),
            "Impodo server migrate completed: schema version 1; applied 1.",
        )
        migrate.assert_called_once()

    def test_safe_exit_codes_distinguish_retry_refusal_and_execution(self) -> None:
        cases = (
            (ServerMigrationUnavailableError("private-host"), TEMPORARY_FAILURE_EXIT),
            (
                ServerMigrationIncompatibleError("future-generation"),
                CONFIGURATION_ERROR_EXIT,
            ),
            (ServerMigrationExecutionError("secret statement"), SOFTWARE_ERROR_EXIT),
        )
        for error, expected_exit in cases:
            with self.subTest(expected_exit=expected_exit):
                output = StringIO()
                with (
                    patch.dict("os.environ", _migration_environment(), clear=True),
                    patch(
                        "impodo.web.composition.server_cli."
                        "run_file_secret_linux_postgresql_migrations",
                        side_effect=error,
                    ),
                    redirect_stderr(output),
                ):
                    exit_code = main(("migrate",))

                self.assertEqual(exit_code, expected_exit)
                self.assertNotIn(str(error), output.getvalue())
                self.assertNotIn("private-host", output.getvalue())
                self.assertNotIn("future-generation", output.getvalue())
                self.assertNotIn("secret statement", output.getvalue())

    def test_linux_composition_reads_only_the_migration_secret(self) -> None:
        settings = load_server_migration_settings(_migration_environment())
        file_operations = _FileOperations()
        session = _MigrationSession()
        opened_dsns: list[str] = []

        def open_session(dsn: str) -> _MigrationSession:
            opened_dsns.append(dsn)
            return session

        result = run_file_secret_linux_postgresql_migrations(
            settings,
            secret_file_operations=file_operations,
            postgresql_session_factory=open_session,
        )

        self.assertEqual(
            result.applied_versions,
            tuple(migration.version for migration in POSTGRESQL_MIGRATIONS),
        )
        self.assertEqual(opened_dsns, ["private-migration-dsn"])
        self.assertEqual(file_operations.opened_child, "postgres-migration-dsn")
        self.assertEqual(file_operations.closed, [11, 10])
        self.assertTrue(session.committed)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
