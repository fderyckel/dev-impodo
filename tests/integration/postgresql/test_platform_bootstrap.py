"""Verify first-operator persistence uses one secret-safe transaction."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import PurePosixPath
from unittest.mock import patch

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.platform_bootstrap import (
    POSTGRESQL_BOOTSTRAP_CONNECTION_BUDGET,
    POSTGRESQL_BOOTSTRAP_QUERY_BUDGET,
    PsycopgPlatformAdministratorBootstrapRepository,
    open_psycopg_platform_bootstrap_session,
)
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionOutcome,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorAlreadyBootstrappedError,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
EVENT_ID = "20000000-0000-4000-8000-000000000001"


class _Session:
    def __init__(self, responses: list[tuple[object, ...] | None]) -> None:
        self.responses = responses
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, query, parameters=()) -> None:
        self.events.append(("execute", query, parameters))

    def fetch_one(self, query, parameters=()):
        self.events.append(("fetch_one", query, parameters))
        return self.responses.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _record() -> PlatformAdministratorBootstrapRecord:
    return PlatformAdministratorBootstrapRecord(
        administrator_id=ADMINISTRATOR_ID,
        normalized_login_email="admin@example.test",
        display_name="Platform operator",
        status=PlatformAdministratorStatus.ACTIVE,
        security_revision=1,
        password_hash=PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=b"0123456789abcdef",
            digest=b"h" * 32,
        ),
        created_at=NOW,
    )


def _event() -> BootstrapSecurityEvent:
    return BootstrapSecurityEvent(
        event_id=EVENT_ID,
        deployment_id="local-trial",
        kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
        subject_administrator_id=ADMINISTRATOR_ID,
        occurred_at=NOW,
    )


def _repository(
    session: _Session,
    *,
    metrics: list[PostgreSQLTransactionMetric] | None = None,
) -> PsycopgPlatformAdministratorBootstrapRepository:
    return PsycopgPlatformAdministratorBootstrapRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
        metrics_observer=metrics.append if metrics is not None else None,
    )


class PostgreSQLPlatformBootstrapTests(unittest.TestCase):
    def test_bootstrap_serializes_checks_and_inserts_one_atomic_bundle(self) -> None:
        session = _Session([None, None])
        metrics: list[PostgreSQLTransactionMetric] = []

        _repository(session, metrics=metrics).bootstrap(_record(), _event())

        self.assertEqual(len(session.events), POSTGRESQL_BOOTSTRAP_QUERY_BUDGET)
        self.assertIn("pg_advisory_xact_lock", session.events[0][1])
        self.assertIn("status = 'ACTIVE'", session.events[1][1])
        self.assertIn("platform_administrator", session.events[2][1])
        self.assertIn("login_identity", session.events[3][1])
        self.assertIn("password_identity", session.events[4][1])
        self.assertIn("platform_security_event", session.events[5][1])
        all_parameters = repr(tuple(event[2] for event in session.events))
        self.assertNotIn("fictional-bootstrap-password", all_parameters)
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)
        self.assertTrue(session.closed)
        self.assertEqual(len(metrics), 1)
        self.assertEqual(
            metrics[0].outcome,
            PostgreSQLTransactionOutcome.COMMITTED,
        )
        self.assertEqual(
            metrics[0].connection_count,
            POSTGRESQL_BOOTSTRAP_CONNECTION_BUDGET,
        )
        self.assertEqual(metrics[0].query_count, POSTGRESQL_BOOTSTRAP_QUERY_BUDGET)

    def test_existing_active_operator_refuses_before_any_insert(self) -> None:
        session = _Session([None, (ADMINISTRATOR_ID,)])

        with self.assertRaisesRegex(
            PlatformAdministratorAlreadyBootstrappedError,
            "already exists",
        ):
            _repository(session).bootstrap(_record(), _event())

        self.assertEqual([event[0] for event in session.events], ["fetch_one"] * 2)
        self.assertFalse(session.committed)
        self.assertTrue(session.rolled_back)
        self.assertTrue(session.closed)

    def test_default_session_is_one_bounded_runtime_role_transaction(self) -> None:
        connection = unittest.mock.Mock()
        with patch(
            "impodo.adapters.postgresql.platform_bootstrap.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_platform_bootstrap_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["application_name"], "impodo-platform-bootstrap")
        self.assertIn(f"role={POSTGRESQL_RUNTIME_ROLE}", kwargs["options"])
        self.assertIn("statement_timeout=10000", kwargs["options"])
        self.assertIn("lock_timeout=5000", kwargs["options"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
