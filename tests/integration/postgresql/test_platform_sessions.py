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
ORGANIZATION_ADMINISTRATOR_ID = "40000000-0000-4000-8000-000000000004"
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


def _organization_subject() -> SessionSubject:
    return SessionSubject(
        principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        principal_id=ORGANIZATION_ADMINISTRATOR_ID,
        organization_id="acme",
    )


def _organization_record() -> ServerSessionRecord:
    record = _record()
    return ServerSessionRecord(
        session_id=record.session_id,
        subject=_organization_subject(),
        authentication_method=record.authentication_method,
        security_revision=record.security_revision,
        handle_digest=record.handle_digest,
        csrf_digest=record.csrf_digest,
        created_at=record.created_at,
        last_seen_at=record.last_seen_at,
        idle_expires_at=record.idle_expires_at,
        absolute_expires_at=record.absolute_expires_at,
        elevated_until=record.elevated_until,
        optimistic_revision=record.optimistic_revision,
    )


def _event(kind: SecurityEventKind) -> SessionSecurityEvent:
    return SessionSecurityEvent(
        event_id="30000000-0000-4000-8000-000000000003",
        deployment_id="local-trial",
        kind=kind,
        subject=None,
        actor=None,
        occurred_at=NOW,
    )


def _row(
    *,
    state: str = "ACTIVE",
    idle_expires_at: datetime | None = None,
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
    )


def _platform_responses(
    row: tuple[object, ...],
    *,
    current_security_revision: int = 3,
) -> list[tuple[object, ...]]:
    return [
        ("PLATFORM_ADMINISTRATOR", ADMINISTRATOR_ID, None),
        ("ACTIVE", current_security_revision),
        row,
    ]


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

    def test_customer_session_rechecks_tenant_assignment_and_uses_generic_event(self) -> None:
        create_session = _Session(responses=[("ACTIVE", 3, "ACTIVE")])
        _repository(create_session).create(
            _organization_record(),
            _event(SecurityEventKind.SESSION_CREATED),
        )

        create_queries = "\n".join(event[1] for event in create_session.events)
        self.assertIn("organization_administrator_assignment", create_queries)
        self.assertIn("server_security_event", create_queries)
        session_insert = next(
            event
            for event in create_session.events
            if "INSERT INTO impodo_control.server_session" in event[1]
        )
        self.assertIn("acme", session_insert[2])

        customer_row = list(_row())
        customer_row[0] = "ORGANIZATION_ADMINISTRATOR"
        customer_row[1] = ORGANIZATION_ADMINISTRATOR_ID
        customer_row[2] = "acme"
        resolve_session = _Session(
            responses=[
                (
                    "ORGANIZATION_ADMINISTRATOR",
                    ORGANIZATION_ADMINISTRATOR_ID,
                    "acme",
                ),
                ("ACTIVE", 3, "ACTIVE"),
                tuple(customer_row),
            ],
            affected=[1],
        )
        current = _repository(resolve_session).resolve(
            proof=SessionProof(session_id=SESSION_ID, handle_digest=b"h" * 32),
            accessed_at=NOW + timedelta(minutes=1),
            proposed_idle_expires_at=NOW + timedelta(minutes=31),
        )
        assert current is not None
        self.assertEqual(current.subject.organization_id, "acme")

        replaced = _Session(
            responses=[
                (
                    "ORGANIZATION_ADMINISTRATOR",
                    ORGANIZATION_ADMINISTRATOR_ID,
                    "acme",
                ),
                None,
            ]
        )
        self.assertIsNone(
            _repository(replaced).resolve(
                proof=SessionProof(session_id=SESSION_ID, handle_digest=b"h" * 32),
                accessed_at=NOW,
                proposed_idle_expires_at=NOW + timedelta(minutes=30),
            )
        )

    def test_resolve_touches_valid_session_but_bad_csrf_does_not_mutate(self) -> None:
        valid_session = _Session(responses=_platform_responses(_row()), affected=[1])
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

        invalid_session = _Session(responses=_platform_responses(_row()))
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
            (_row(), SessionRevocationReason.SECURITY_REVISION),
        ):
            with self.subTest(reason=reason):
                session = _Session(
                    responses=_platform_responses(
                        row,
                        current_security_revision=(
                            4
                            if reason is SessionRevocationReason.SECURITY_REVISION
                            else 3
                        ),
                    ),
                    affected=[1],
                )
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
        session = _Session(responses=_platform_responses(_row()), affected=[1])
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

        stale = _Session(responses=_platform_responses(_row()))
        with self.assertRaises(ServerSessionConflictError):
            _repository(stale).revoke(
                RevokeServerSessionCommand(
                    actor=_subject(),
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
