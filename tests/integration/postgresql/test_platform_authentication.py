"""Verify bounded PostgreSQL persistence for platform login facts."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import PurePosixPath
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.platform_authentication import (
    POSTGRESQL_AUTHENTICATION_LOAD_QUERY_BUDGET,
    POSTGRESQL_AUTHENTICATION_REJECTION_QUERY_BUDGET,
    POSTGRESQL_AUTHENTICATION_SUCCESS_QUERY_BUDGET,
    PsycopgPlatformAuthenticationRepository,
    open_psycopg_platform_authentication_session,
)
from impodo.adapters.postgresql.transaction_metrics import (
    PostgreSQLTransactionMetric,
    PostgreSQLTransactionOutcome,
)
from impodo.domain.identity.authentication import (
    AuthenticationSecurityEvent,
    AuthenticationThrottlePolicy,
)
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind

NOW = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
ACCOUNT_DIGEST = "sha256:" + "a" * 64
SOURCE_DIGEST = "sha256:" + "b" * 64


class _Session:
    def __init__(
        self,
        responses: list[tuple[object, ...] | None] | None = None,
        *,
        affected: int = 1,
    ) -> None:
        self.responses = responses or []
        self.affected = affected
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, query, parameters=()) -> None:
        self.events.append(("execute", query, parameters))

    def execute_affected(self, query, parameters=()) -> int:
        self.events.append(("execute_affected", query, parameters))
        return self.affected

    def fetch_one(self, query, parameters=()):
        self.events.append(("fetch_one", query, parameters))
        return self.responses.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _repository(
    session: _Session,
    *,
    metrics: list[PostgreSQLTransactionMetric] | None = None,
) -> PsycopgPlatformAuthenticationRepository:
    return PsycopgPlatformAuthenticationRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
        metrics_observer=metrics.append if metrics is not None else None,
    )


def _event(
    kind: SecurityEventKind, *, subject: str | None
) -> AuthenticationSecurityEvent:
    return AuthenticationSecurityEvent(
        event_id=f"event-{kind.value.lower()}",
        deployment_id="local-trial",
        kind=kind.value,
        subject_administrator_id=subject,
        principal_hint_digest=ACCOUNT_DIGEST,
        source_digest=SOURCE_DIGEST,
        occurred_at=NOW,
    )


def _password_hash() -> PasswordHash:
    return PasswordHash(
        algorithm="scrypt",
        parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
        salt=b"s" * 16,
        digest=b"h" * 32,
    )


class PostgreSQLPlatformAuthenticationTests(unittest.TestCase):
    def test_load_reads_credential_and_both_locks_in_one_query(self) -> None:
        session = _Session(
            [
                (
                    ADMINISTRATOR_ID,
                    "ACTIVE",
                    3,
                    "scrypt",
                    {"n": 32_768, "r": 8, "p": 1, "dklen": 32},
                    b"s" * 16,
                    b"h" * 32,
                    2,
                    False,
                )
            ]
        )
        metrics: list[PostgreSQLTransactionMetric] = []

        attempt = _repository(session, metrics=metrics).load_attempt(
            normalized_login_email="operator@example.test",
            account_digest=ACCOUNT_DIGEST,
            source_digest=SOURCE_DIGEST,
            attempted_at=NOW,
        )

        self.assertIsNotNone(attempt.credential)
        assert attempt.credential is not None
        self.assertEqual(attempt.credential.administrator_id, ADMINISTRATOR_ID)
        self.assertEqual(attempt.credential.password_revision, 2)
        self.assertFalse(attempt.throttled)
        self.assertEqual(
            len(session.events), POSTGRESQL_AUTHENTICATION_LOAD_QUERY_BUDGET
        )
        self.assertIn("scope_kind = 'ACCOUNT'", session.events[0][1])
        self.assertIn("scope_kind = 'SOURCE'", session.events[0][1])
        self.assertTrue(session.committed)
        self.assertTrue(session.closed)
        self.assertEqual(
            metrics[0].query_count, POSTGRESQL_AUTHENTICATION_LOAD_QUERY_BUDGET
        )

    def test_unknown_account_remains_opaque_while_source_lock_applies(self) -> None:
        session = _Session([(None, None, None, None, None, None, None, None, True)])

        attempt = _repository(session).load_attempt(
            normalized_login_email="unknown@example.test",
            account_digest=ACCOUNT_DIGEST,
            source_digest=SOURCE_DIGEST,
            attempted_at=NOW,
        )

        self.assertIsNone(attempt.credential)
        self.assertTrue(attempt.throttled)

    def test_rejection_appends_event_and_updates_account_and_source(self) -> None:
        session = _Session()
        metrics: list[PostgreSQLTransactionMetric] = []

        _repository(session, metrics=metrics).record_rejection(
            _event(SecurityEventKind.AUTHENTICATION_REJECTED, subject=None),
            count_failure=True,
            policy=AuthenticationThrottlePolicy(),
        )

        self.assertEqual(
            len(session.events),
            POSTGRESQL_AUTHENTICATION_REJECTION_QUERY_BUDGET,
        )
        self.assertIn("platform_security_event", session.events[0][1])
        self.assertEqual(session.events[1][2][0], "ACCOUNT")
        self.assertEqual(session.events[2][2][0], "SOURCE")
        all_parameters = repr(tuple(event[2] for event in session.events))
        self.assertNotIn("unknown@example.test", all_parameters)
        self.assertNotIn("127.0.0.1", all_parameters)
        self.assertEqual(
            metrics[0].outcome,
            PostgreSQLTransactionOutcome.COMMITTED,
        )

    def test_success_resets_throttles_and_rehashes_with_revision_predicate(
        self,
    ) -> None:
        session = _Session(affected=1)
        metrics: list[PostgreSQLTransactionMetric] = []

        rehashed = _repository(session, metrics=metrics).record_success(
            _event(
                SecurityEventKind.AUTHENTICATION_SUCCEEDED, subject=ADMINISTRATOR_ID
            ),
            expected_password_revision=2,
            replacement_hash=_password_hash(),
            rehash_event=_event(
                SecurityEventKind.PASSWORD_REHASHED,
                subject=ADMINISTRATOR_ID,
            ),
        )

        self.assertTrue(rehashed)
        self.assertEqual(
            len(session.events),
            POSTGRESQL_AUTHENTICATION_SUCCESS_QUERY_BUDGET,
        )
        rehash = session.events[1]
        self.assertIn("password_revision = password_revision + 1", rehash[1])
        self.assertIn("password_revision = %s", rehash[1])
        self.assertEqual(rehash[2][-1], 2)
        self.assertEqual(
            metrics[0].query_count, POSTGRESQL_AUTHENTICATION_SUCCESS_QUERY_BUDGET
        )

    def test_stale_rehash_does_not_claim_password_rehashed(self) -> None:
        session = _Session(affected=0)

        rehashed = _repository(session).record_success(
            _event(
                SecurityEventKind.AUTHENTICATION_SUCCEEDED, subject=ADMINISTRATOR_ID
            ),
            expected_password_revision=2,
            replacement_hash=_password_hash(),
            rehash_event=_event(
                SecurityEventKind.PASSWORD_REHASHED,
                subject=ADMINISTRATOR_ID,
            ),
        )

        self.assertFalse(rehashed)
        event_kinds = tuple(
            event[2][2]
            for event in session.events
            if "platform_security_event" in event[1]
        )
        self.assertEqual(
            event_kinds, (SecurityEventKind.AUTHENTICATION_SUCCEEDED.value,)
        )

    def test_default_session_is_one_bounded_runtime_role_transaction(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.platform_authentication.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_platform_authentication_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["connect_timeout"], 5)
        self.assertEqual(kwargs["application_name"], "impodo-platform-authentication")
        self.assertIn(f"role={POSTGRESQL_RUNTIME_ROLE}", kwargs["options"])
        self.assertIn("statement_timeout=10000", kwargs["options"])
        self.assertIn("lock_timeout=5000", kwargs["options"])
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
