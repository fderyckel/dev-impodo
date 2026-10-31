"""Prove secure DSN resolution and bounded PostgreSQL startup inspection."""

from __future__ import annotations

from pathlib import PurePosixPath
from types import SimpleNamespace
import stat
import unittest
from typing import LiteralString
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.startup import (
    POSTGRESQL_CONTROL_SCHEMA_GENERATION,
    POSTGRESQL_CONTROL_SCHEMA_VERSION,
    PostgreSQLDsnError,
    PostgreSQLSchemaCompatibilityError,
    PostgreSQLStatementError,
    PostgreSQLTransportError,
    PostgreSQLUnavailableError,
    PsycopgPostgreSQLStartupInspector,
    open_psycopg_startup_session,
)
from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.secrets.linux_files import (
    LinuxSecretFileError,
    LinuxSecretFileReader,
)


class _FileOperations:
    def __init__(
        self,
        payload: bytes,
        *,
        root_mode: int = stat.S_IFDIR | 0o750,
        root_uid: int = 0,
        root_gid: int = 10001,
        secret_mode: int = stat.S_IFREG | 0o640,
        secret_uid: int = 0,
        secret_gid: int = 10001,
        secret_nlink: int = 1,
        close_failures: frozenset[int] = frozenset(),
    ) -> None:
        self.payload = payload
        self.root_metadata = SimpleNamespace(
            st_mode=root_mode,
            st_uid=root_uid,
            st_gid=root_gid,
            st_nlink=1,
            st_size=0,
        )
        self.secret_metadata = SimpleNamespace(
            st_mode=secret_mode,
            st_uid=secret_uid,
            st_gid=secret_gid,
            st_nlink=secret_nlink,
            st_size=len(payload),
        )
        self.closed: list[int] = []
        self.close_failures = close_failures

    def open_directory(self, path: PurePosixPath) -> int:
        if path != PurePosixPath("/run/secrets"):
            raise AssertionError(path)
        return 10

    def open_child(self, directory_fd: int, name: str) -> int:
        if (directory_fd, name) != (10, "postgres-dsn"):
            raise AssertionError((directory_fd, name))
        return 11

    def metadata(self, descriptor: int) -> SimpleNamespace:
        return self.root_metadata if descriptor == 10 else self.secret_metadata

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        if descriptor != 11 or maximum_bytes <= len(self.payload):
            raise AssertionError((descriptor, maximum_bytes))
        return self.payload

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        if descriptor in self.close_failures:
            raise OSError("private secret path")


class _Session:
    def __init__(
        self,
        responses: list[tuple[tuple[object, ...], ...] | Exception],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False
        self.close_error = close_error

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        self.queries.append((query, parameters))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class LinuxSecretFileTests(unittest.TestCase):
    def test_root_owned_group_readable_direct_child_is_read_once(self) -> None:
        payload = b"postgresql://impodo:encoded%20secret@database/impodo\n"
        operations = _FileOperations(payload)
        reader = LinuxSecretFileReader(
            expected_gid=10001,
            operations=operations,
        )

        value = reader.read_text(
            PurePosixPath("/run/secrets"),
            PurePosixPath("/run/secrets/postgres-dsn"),
        )

        self.assertEqual(
            value,
            "postgresql://impodo:encoded%20secret@database/impodo",
        )
        self.assertEqual(operations.closed, [11, 10])

    def test_secret_policy_rejects_links_ownership_and_broad_permissions(self) -> None:
        cases = (
            (
                _FileOperations(b"dsn", secret_mode=stat.S_IFLNK | 0o640),
                "not_regular_file",
            ),
            (
                _FileOperations(b"dsn", secret_uid=10001),
                "not_owned_by_root",
            ),
            (
                _FileOperations(b"dsn", secret_mode=stat.S_IFREG | 0o644),
                "permissions_too_broad",
            ),
            (
                _FileOperations(b"dsn", secret_nlink=2),
                "unexpected_link_count",
            ),
        )
        for operations, code in cases:
            with self.subTest(code=code):
                reader = LinuxSecretFileReader(
                    expected_gid=10001,
                    operations=operations,
                )
                with self.assertRaises(LinuxSecretFileError) as caught:
                    reader.read_text(
                        PurePosixPath("/run/secrets"),
                        PurePosixPath("/run/secrets/postgres-dsn"),
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(operations.closed, [11, 10])

    def test_secret_reference_must_be_a_direct_child_and_one_line(self) -> None:
        reader = LinuxSecretFileReader(
            expected_gid=10001,
            operations=_FileOperations(b"first\nsecond"),
        )

        for reference, code in (
            (PurePosixPath("/run/secrets/nested/postgres-dsn"), "outside_secret_root"),
            (PurePosixPath("/run/secrets/postgres-dsn"), "invalid_text"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(LinuxSecretFileError) as caught:
                    reader.read_text(PurePosixPath("/run/secrets"), reference)
                self.assertEqual(caught.exception.code, code)

    def test_close_failure_is_bounded_and_both_descriptors_are_closed(self) -> None:
        operations = _FileOperations(
            b"private-dsn",
            close_failures=frozenset({11}),
        )
        reader = LinuxSecretFileReader(expected_gid=10001, operations=operations)

        with self.assertRaises(LinuxSecretFileError) as caught:
            reader.read_text(
                PurePosixPath("/run/secrets"),
                PurePosixPath("/run/secrets/postgres-dsn"),
            )

        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(operations.closed, [11, 10])


class PostgreSQLStartupInspectorTests(unittest.TestCase):
    def test_one_session_proves_reachability_then_exact_schema(self) -> None:
        session = _Session(
            [
                ((1, POSTGRESQL_RUNTIME_ROLE),),
                (
                    (
                        POSTGRESQL_CONTROL_SCHEMA_GENERATION,
                        POSTGRESQL_CONTROL_SCHEMA_VERSION,
                    ),
                ),
            ]
        )
        opened_dsns: list[str] = []

        def open_session(dsn: str) -> _Session:
            opened_dsns.append(dsn)
            return session

        inspector = PsycopgPostgreSQLStartupInspector(
            lambda _reference: "postgresql://operator:secret@database/impodo",
            session_factory=open_session,
        )

        inspector.inspect(PurePosixPath("/run/secrets/postgres-dsn"))

        self.assertEqual(len(opened_dsns), 1)
        self.assertEqual(session.queries[0], ("SELECT 1, current_user", ()))
        self.assertIn("impodo_control.schema_contract", session.queries[1][0])
        self.assertEqual(session.queries[1][1], ("control",))
        self.assertTrue(session.closed)

    def test_dsn_and_transport_failures_expose_no_connection_detail(self) -> None:
        def fail_dsn(_reference: PurePosixPath) -> str:
            raise PostgreSQLDsnError("postgresql://operator:secret@database")

        inspector = PsycopgPostgreSQLStartupInspector(fail_dsn)
        with self.assertRaises(PostgreSQLUnavailableError) as caught:
            inspector.inspect(PurePosixPath("/run/secrets/postgres-dsn"))
        self.assertNotIn("secret", str(caught.exception))

        session = _Session([PostgreSQLTransportError("database.internal")])
        inspector = PsycopgPostgreSQLStartupInspector(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )
        with self.assertRaises(PostgreSQLUnavailableError) as caught:
            inspector.inspect(PurePosixPath("/run/secrets/postgres-dsn"))
        self.assertNotIn("internal", str(caught.exception))
        self.assertTrue(session.closed)

    def test_privileged_or_unexpected_database_role_is_not_runtime_ready(self) -> None:
        session = _Session([((1, "impodo_migrator"),)])
        inspector = PsycopgPostgreSQLStartupInspector(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )

        with self.assertRaises(PostgreSQLUnavailableError) as caught:
            inspector.inspect(PurePosixPath("/run/secrets/postgres-runtime-dsn"))

        self.assertNotIn("migrator", str(caught.exception))
        self.assertTrue(session.closed)

    def test_schema_statement_or_value_mismatch_is_incompatible(self) -> None:
        cases: tuple[tuple[tuple[object, ...], ...] | Exception, ...] = (
            PostgreSQLStatementError("relation name"),
            (),
            ((POSTGRESQL_CONTROL_SCHEMA_GENERATION, 0),),
            (("future-generation", 99),),
        )
        for response in cases:
            with self.subTest(schema_response=response):
                session = _Session([((1, POSTGRESQL_RUNTIME_ROLE),), response])
                inspector = PsycopgPostgreSQLStartupInspector(
                    lambda _reference: "private-dsn",
                    session_factory=lambda _dsn, session=session: session,
                )
                with self.assertRaises(PostgreSQLSchemaCompatibilityError) as caught:
                    inspector.inspect(PurePosixPath("/run/secrets/postgres-dsn"))
                self.assertNotIn("future-generation", str(caught.exception))
                self.assertTrue(session.closed)

    def test_connection_close_failure_is_bounded(self) -> None:
        session = _Session(
            [
                ((1, POSTGRESQL_RUNTIME_ROLE),),
                (
                    (
                        POSTGRESQL_CONTROL_SCHEMA_GENERATION,
                        POSTGRESQL_CONTROL_SCHEMA_VERSION,
                    ),
                ),
            ],
            close_error=PostgreSQLTransportError("database.internal"),
        )
        inspector = PsycopgPostgreSQLStartupInspector(
            lambda _reference: "private-dsn",
            session_factory=lambda _dsn: session,
        )

        with self.assertRaises(PostgreSQLUnavailableError) as caught:
            inspector.inspect(PurePosixPath("/run/secrets/postgres-dsn"))

        self.assertNotIn("internal", str(caught.exception))
        self.assertTrue(session.closed)

    def test_default_psycopg_session_is_bounded_read_only_and_autocommit(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.startup.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_startup_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertTrue(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["application_name"], "impodo-startup")
        self.assertIn(f"role={POSTGRESQL_RUNTIME_ROLE}", kwargs["options"])
        self.assertIn("default_transaction_read_only=on", kwargs["options"])
        self.assertIn("statement_timeout=5000", kwargs["options"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
