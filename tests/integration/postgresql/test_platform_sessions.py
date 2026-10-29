"""Verify atomic PostgreSQL persistence for opaque server sessions."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.platform_sessions import (
    PsycopgServerSessionRepository,
    open_psycopg_server_session,
)
from impodo.domain.identity.bootstrap import SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    RevokeServerSessionCommand,
    ServerSessionConflictError,
    ServerSessionRecord,
    SessionAuthenticationMethod,
    SessionProof,
    SessionRevocationReason,
    SessionSecretMaterial,
    SessionSecurityEvent,
    SessionSubject,
)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
SESSION_ID = "20000000-0000-4000-8000-000000000002"


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
        self.closed = False

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
        self.closed = True


def _repository(session: _Session) -> PsycopgServerSessionRepository:
    return PsycopgServerSessionRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


def _subject() -> SessionSubject:
    return SessionSubject(
        principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
        principal_id=ADMINISTRATOR_ID,
        organization_id=None,
    )


def _record() -> ServerSessionRecord:
    return ServerSessionRecord(
        session_id=SESSION_ID,
        subject=_subject(),
        authentication_method=SessionAuthenticationMethod.PASSWORD,
        security_revision=3,
        handle_digest=b"h" * 32,
        csrf_digest=b"c" * 32,
        created_at=NOW,
        last_seen_at=NOW,
        idle_expires_at=NOW + timedelta(minutes=30),
        absolute_expires_at=NOW + timedelta(hours=12),
        elevated_until=None,
        optimistic_revision=1,
    )


def _event(kind: SecurityEventKind) -> SessionSecurityEvent:
    return SessionSecurityEvent(
        event_id="30000000-0000-4000-8000-000000000003",
        deployment_id="local-trial",
        kind=kind,
        subject_administrator_id=None,
        actor_administrator_id=None,
        occurred_at=NOW,
    )


def _row(
    *,
    state: str = "ACTIVE",
    idle_expires_at: datetime | None = None,
    current_security_revision: int = 3,
) -> tuple[object, ...]:
    return (
        "PLATFORM_ADMINISTRATOR",
        ADMINISTRATOR_ID,
        None,
        b"h" * 32,
        b"c" * 32,
        "PASSWORD",
        3,
        state,
        NOW,
        NOW,
        idle_expires_at or NOW + timedelta(minutes=30),
        NOW + timedelta(hours=12),
        None,
        1,
        "ACTIVE",
        current_security_revision,
    )


class PostgreSQLServerSessionTests(unittest.TestCase):
    def test_create_locks_subject_and_persists_only_digests_and_event(self) -> None:
        session = _Session(responses=[("ACTIVE", 3)])

        _repository(session).create(_record(), _event(SecurityEventKind.SESSION_CREATED))

        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("FOR UPDATE", queries)
        self.assertIn("INSERT INTO impodo_control.server_session", queries)
        self.assertIn("platform_security_event", queries)
        parameters = tuple(
            parameter
            for event in session.events
            for parameter in event[2]
        )
        self.assertIn(b"h" * 32, parameters)
        self.assertIn(b"c" * 32, parameters)
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)

    def test_resolve_touches_valid_session_but_bad_csrf_does_not_mutate(self) -> None:
        valid_session = _Session(responses=[_row()], affected=[1])
        current = _repository(valid_session).resolve(
            proof=SessionProof(
                session_id=SESSION_ID,
                handle_digest=b"h" * 32,
                csrf_digest=b"c" * 32,
            ),
            accessed_at=NOW + timedelta(minutes=1),
            proposed_idle_expires_at=NOW + timedelta(minutes=31),
        )

        assert current is not None
        self.assertEqual(current.optimistic_revision, 2)
        self.assertTrue(
            any("SET last_seen_at" in event[1] for event in valid_session.events)
        )

        invalid_session = _Session(responses=[_row()])
        rejected = _repository(invalid_session).resolve(
            proof=SessionProof(
                session_id=SESSION_ID,
                handle_digest=b"h" * 32,
                csrf_digest=b"x" * 32,
            ),
            accessed_at=NOW + timedelta(minutes=1),
            proposed_idle_expires_at=NOW + timedelta(minutes=31),
        )
        self.assertIsNone(rejected)
        self.assertFalse(
            any(event[0] == "execute_affected" for event in invalid_session.events)
        )
        self.assertTrue(invalid_session.committed)

    def test_expiry_or_security_revision_change_revokes_in_same_transaction(self) -> None:
        for row, reason in (
            (_row(idle_expires_at=NOW), SessionRevocationReason.EXPIRED),
            (
                _row(current_security_revision=4),
                SessionRevocationReason.SECURITY_REVISION,
            ),
        ):
            with self.subTest(reason=reason):
                session = _Session(responses=[row], affected=[1])
                current = _repository(session).resolve(
                    proof=SessionProof(
                        session_id=SESSION_ID,
                        handle_digest=b"h" * 32,
                    ),
                    accessed_at=NOW,
                    proposed_idle_expires_at=NOW + timedelta(minutes=30),
                )

                self.assertIsNone(current)
                revoke = next(
                    event
                    for event in session.events
                    if event[0] == "execute_affected"
                )
                self.assertEqual(revoke[2][1], reason.value)
                self.assertTrue(session.committed)

    def test_rotation_is_atomic_and_stale_admin_revocation_rolls_back(self) -> None:
        session = _Session(responses=[_row()], affected=[1])
        rotated = _repository(session).rotate(
            proof=SessionProof(session_id=SESSION_ID, handle_digest=b"h" * 32),
            replacement=SessionSecretMaterial(
                session_id=SESSION_ID,
                handle="replacement",
                handle_digest=b"n" * 32,
                csrf_token="replacement-csrf",
                csrf_digest=b"z" * 32,
            ),
            authentication_method=SessionAuthenticationMethod.WEBAUTHN,
            expected_security_revision=3,
            rotated_at=NOW + timedelta(minutes=1),
            proposed_idle_expires_at=NOW + timedelta(minutes=31),
            elevated_until=NOW + timedelta(minutes=11),
            event=_event(SecurityEventKind.SESSION_ROTATED),
        )

        assert rotated is not None
        self.assertEqual(rotated.authentication_method, SessionAuthenticationMethod.WEBAUTHN)
        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("SET handle_digest", queries)
        self.assertIn("SESSION_ROTATED", str(tuple(event[2] for event in session.events)))

        stale = _Session(responses=[("ACTIVE", 3), _row()])
        with self.assertRaises(ServerSessionConflictError):
            _repository(stale).revoke(
                RevokeServerSessionCommand(
                    actor_administrator_id=ADMINISTRATOR_ID,
                    session_id=SESSION_ID,
                    expected_revision=99,
                ),
                _event(SecurityEventKind.SESSION_REVOKED),
                revoked_at=NOW,
            )
        self.assertTrue(stale.rolled_back)
        self.assertFalse(stale.committed)

    def test_default_session_uses_one_runtime_role_transaction(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.platform_sessions.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_server_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["application_name"], "impodo-server-session")
        self.assertIn(f"role={POSTGRESQL_RUNTIME_ROLE}", kwargs["options"])
        self.assertIn("statement_timeout=10000", kwargs["options"])
        self.assertIn("lock_timeout=5000", kwargs["options"])


if __name__ == "__main__":
    unittest.main()
