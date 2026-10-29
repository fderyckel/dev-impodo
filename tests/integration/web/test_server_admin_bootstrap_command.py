"""Prove the non-HTTP first-operator command is secret-safe and fail-closed."""

from __future__ import annotations

import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorAlreadyBootstrappedError,
    PlatformAdministratorBootstrapInputError,
    PlatformAdministratorBootstrapStorageError,
    PlatformAdministratorBootstrapUnavailableError,
)
from impodo.web.composition.server_cli import (
    CONFIGURATION_ERROR_EXIT,
    SOFTWARE_ERROR_EXIT,
    TEMPORARY_FAILURE_EXIT,
    build_parser,
    main,
)
from impodo.web.composition.server_config import (
    ServerConfigurationError,
    load_server_admin_settings,
)
from impodo.web.composition.server_linux_admin import (
    run_file_secret_linux_platform_admin_bootstrap,
)


def _admin_environment() -> dict[str, str]:
    return {
        "IMPODO_DEPLOYMENT_ID": "local-trial",
        "IMPODO_POSTGRES_DSN_FILE": "/run/secrets/postgres-runtime-dsn",
        "IMPODO_SECRET_PROVIDER": "files",
        "IMPODO_SECRET_ROOT": "/run/secrets",
    }


def _arguments() -> tuple[str, ...]:
    return (
        "admin",
        "bootstrap",
        "--login-email",
        "admin@example.test",
        "--display-name",
        "Platform operator",
        "--password-file",
        "/run/secrets/platform-admin-password",
    )


class _FileOperations:
    def __init__(self) -> None:
        self.opened_children: list[str] = []
        self.current_child = ""

    def open_directory(self, path: PurePosixPath) -> int:
        if path != PurePosixPath("/run/secrets"):
            raise AssertionError(path)
        return 10

    def open_child(self, directory_fd: int, name: str) -> int:
        if directory_fd != 10:
            raise AssertionError(directory_fd)
        self.current_child = name
        self.opened_children.append(name)
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
        payload = self._payload()
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o640,
            st_uid=0,
            st_gid=10001,
            st_nlink=1,
            st_size=len(payload),
        )

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        payload = self._payload()
        if descriptor != 11 or maximum_bytes <= len(payload):
            raise AssertionError((descriptor, maximum_bytes))
        return payload

    def close(self, _descriptor: int) -> None:
        return None

    def _payload(self) -> bytes:
        if self.current_child == "platform-admin-password":
            return b"fictional-bootstrap-password"
        if self.current_child == "postgres-runtime-dsn":
            return b"private-runtime-dsn"
        raise AssertionError(self.current_child)


class _BootstrapSession:
    def __init__(self) -> None:
        self.responses: list[tuple[object, ...] | None] = [None, None]
        self.committed = False

    def execute(self, _query, _parameters=()) -> None:
        return None

    def fetch_one(self, _query, _parameters=()):
        return self.responses.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        raise AssertionError("successful bootstrap must not roll back")

    def close(self) -> None:
        return None


class _Hasher:
    def __init__(self) -> None:
        self.passwords: list[str] = []

    def hash(self, password: str) -> PasswordHash:
        self.passwords.append(password)
        return PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768),),
            salt=b"0123456789abcdef",
            digest=b"h" * 32,
        )


class ServerAdminBootstrapCommandTests(unittest.TestCase):
    def test_admin_settings_are_narrow_and_require_direct_file_secrets(self) -> None:
        settings = load_server_admin_settings(_admin_environment())

        self.assertEqual(settings.deployment_id, "local-trial")
        self.assertEqual(
            settings.postgres_dsn_file,
            PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        )

        broad = _admin_environment()
        broad["IMPODO_PUBLIC_URL"] = "https://impodo.example.test"
        with self.assertRaisesRegex(ServerConfigurationError, "Unknown"):
            load_server_admin_settings(broad)

        nested = _admin_environment()
        nested["IMPODO_POSTGRES_DSN_FILE"] = "/run/secrets/database/runtime-dsn"
        with self.assertRaisesRegex(ServerConfigurationError, "direct children"):
            load_server_admin_settings(nested)

    def test_command_exposes_no_plain_password_argument(self) -> None:
        help_text = build_parser().format_help()

        self.assertNotIn("--password ", help_text)
        self.assertIn("--password-file", help_text)

    def test_success_reports_no_operator_or_secret_data(self) -> None:
        output = StringIO()
        with (
            patch.dict("os.environ", _admin_environment(), clear=True),
            patch(
                "impodo.web.composition.server_cli."
                "run_file_secret_linux_platform_admin_bootstrap"
            ) as bootstrap,
            redirect_stdout(output),
        ):
            exit_code = main(_arguments())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue().strip(),
            "Impodo server admin bootstrap completed.",
        )
        self.assertNotIn("admin@example.test", output.getvalue())
        bootstrap.assert_called_once()
        self.assertEqual(
            bootstrap.call_args.kwargs["password_file"],
            PurePosixPath("/run/secrets/platform-admin-password"),
        )

    def test_bounded_failures_never_print_private_exception_text(self) -> None:
        cases = (
            (
                PlatformAdministratorBootstrapInputError("fictional-password"),
                CONFIGURATION_ERROR_EXIT,
            ),
            (
                PlatformAdministratorAlreadyBootstrappedError("private-email"),
                CONFIGURATION_ERROR_EXIT,
            ),
            (
                PlatformAdministratorBootstrapUnavailableError("private-host"),
                TEMPORARY_FAILURE_EXIT,
            ),
            (
                PlatformAdministratorBootstrapStorageError("private-hash"),
                SOFTWARE_ERROR_EXIT,
            ),
        )
        for error, expected_exit in cases:
            with self.subTest(expected_exit=expected_exit):
                output = StringIO()
                with (
                    patch.dict("os.environ", _admin_environment(), clear=True),
                    patch(
                        "impodo.web.composition.server_cli."
                        "run_file_secret_linux_platform_admin_bootstrap",
                        side_effect=error,
                    ),
                    redirect_stderr(output),
                ):
                    exit_code = main(_arguments())

                self.assertEqual(exit_code, expected_exit)
                self.assertNotIn(str(error), output.getvalue())
                for private_value in (
                    "fictional-password",
                    "private-email",
                    "private-host",
                    "private-hash",
                ):
                    self.assertNotIn(private_value, output.getvalue())

    def test_missing_identity_input_refuses_before_reading_a_secret(self) -> None:
        output = StringIO()
        with redirect_stderr(output):
            exit_code = main(("admin", "bootstrap"))

        self.assertEqual(exit_code, CONFIGURATION_ERROR_EXIT)
        self.assertIn("login email and display name are required", output.getvalue())

    def test_linux_composition_reads_one_time_file_and_runtime_dsn_once(self) -> None:
        settings = load_server_admin_settings(_admin_environment())
        operations = _FileOperations()
        session = _BootstrapSession()
        hasher = _Hasher()

        run_file_secret_linux_platform_admin_bootstrap(
            settings,
            login_email="admin@example.test",
            display_name="Platform operator",
            password_file=PurePosixPath("/run/secrets/platform-admin-password"),
            secret_file_operations=operations,
            postgresql_session_factory=lambda dsn: (
                session
                if dsn == "private-runtime-dsn"
                else (_ for _ in ()).throw(AssertionError(dsn))
            ),
            password_hasher=hasher,
        )

        self.assertEqual(
            operations.opened_children,
            ["platform-admin-password", "postgres-runtime-dsn"],
        )
        self.assertEqual(hasher.passwords, ["fictional-bootstrap-password"])
        self.assertTrue(session.committed)

    def test_linux_composition_can_prompt_without_a_password_file(self) -> None:
        settings = load_server_admin_settings(_admin_environment())
        operations = _FileOperations()
        session = _BootstrapSession()
        hasher = _Hasher()
        prompts = iter(("fictional-bootstrap-password", "fictional-bootstrap-password"))

        run_file_secret_linux_platform_admin_bootstrap(
            settings,
            login_email="admin@example.test",
            display_name="Platform operator",
            password_file=None,
            secret_file_operations=operations,
            postgresql_session_factory=lambda _dsn: session,
            interactive_secret_reader=lambda _prompt: next(prompts),
            password_hasher=hasher,
        )

        self.assertEqual(operations.opened_children, ["postgres-runtime-dsn"])
        self.assertEqual(hasher.passwords, ["fictional-bootstrap-password"])
        self.assertTrue(session.committed)


if __name__ == "__main__":
    unittest.main()
