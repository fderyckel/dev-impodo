"""Verify bounded PostgreSQL persistence for global server authentication."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import PurePosixPath

from impodo.adapters.postgresql.server_authentication import (
    PsycopgServerAuthenticationRepository,
)
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.server_authentication import (
    ServerAuthenticationSecurityEvent,
)

NOW = datetime(2026, 10, 29, 21, 0, tzinfo=UTC)
SOURCE_DIGEST = "sha256:" + "b" * 64
ACCOUNT_DIGEST = "sha256:" + "a" * 64
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"


class _Session:
    def __init__(
        self,
        responses: list[tuple[object, ...] | None] | None = None,
        affected: list[int] | None = None,
    ) -> None:
        self.responses = responses or []
        self.affected = affected or []
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.committed = False
        self.rolled_back = False

    def execute(self, query, parameters=()) -> None:
        self.events.append(("execute", query, parameters))

    def execute_affected(self, query, parameters=()) -> int:
        self.events.append(("execute_affected", query, parameters))
        return self.affected.pop(0)

    def fetch_one(self, query, parameters=()):
        self.events.append(("fetch_one", query, parameters))
        return self.responses.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        pass


def _repository(session: _Session) -> PsycopgServerAuthenticationRepository:
    return PsycopgServerAuthenticationRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


def _event(kind: SecurityEventKind) -> ServerAuthenticationSecurityEvent:
    return ServerAuthenticationSecurityEvent(
        event_id="20000000-0000-4000-8000-000000000002",
        deployment_id="local-trial",
        kind=kind,
        principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        principal_id=ADMINISTRATOR_ID,
        organization_id="acme",
        principal_hint_digest=ACCOUNT_DIGEST,
        source_digest=SOURCE_DIGEST,
        occurred_at=NOW,
    )


class PostgreSQLServerAuthenticationTests(unittest.TestCase):
    def test_customer_attempt_requires_current_assignment_and_returns_tenant(self) -> None:
        session = _Session(
            responses=[
                (
                    "ORGANIZATION_ADMINISTRATOR",
                    ADMINISTRATOR_ID,
                    "acme",
                    3,
                    "scrypt",
                    {"n": 32768, "r": 8, "p": 1, "dklen": 32},
                    b"s" * 16,
                    b"h" * 32,
                    2,
                    True,
                    False,
                )
            ]
        )

        attempt = _repository(session).load_attempt(
            normalized_login_email="admin@acme.test",
            account_digest=ACCOUNT_DIGEST,
            source_digest=SOURCE_DIGEST,
            attempted_at=NOW,
        )

        assert attempt.credential is not None
        self.assertEqual(attempt.credential.organization_id, "acme")
        query = session.events[0][1]
        self.assertIn("organization_administrator_assignment", query)
        self.assertIn("u.status = 'ACTIVE'", query)
        self.assertIn("o.status = 'ACTIVE'", query)
        self.assertTrue(session.committed)

    def test_customer_success_rehashes_customer_table_and_records_digest_event(self) -> None:
        session = _Session(affected=[1])
        replacement = PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=b"n" * 16,
            digest=b"r" * 32,
        )

        rehashed = _repository(session).record_success(
            _event(SecurityEventKind.AUTHENTICATION_SUCCEEDED),
            expected_password_revision=2,
            replacement_hash=replacement,
            rehash_event=_event(SecurityEventKind.PASSWORD_REHASHED),
        )

        queries = "\n".join(event[1] for event in session.events)
        self.assertTrue(rehashed)
        self.assertIn("customer_user_password_identity", queries)
        self.assertIn("server_security_event", queries)
        parameters = tuple(parameter for event in session.events for parameter in event[2])
        self.assertIn(ACCOUNT_DIGEST, parameters)
        self.assertIn(SOURCE_DIGEST, parameters)


if __name__ == "__main__":
    unittest.main()
