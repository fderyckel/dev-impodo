"""Verify atomic PostgreSQL persistence for account recovery."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from unittest.mock import Mock, patch

from impodo.adapters.postgresql.control_schema import POSTGRESQL_RUNTIME_ROLE
from impodo.adapters.postgresql.platform_recovery import (
    PsycopgAccountRecoveryRepository,
    open_psycopg_account_recovery_session,
)
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind
from impodo.domain.identity.recovery import (
    AccountRecoverySubjectNotEligibleError,
    AuthorizedRecoveryTokenCommand,
    LoginPrincipalKind,
    PasswordResetMutation,
    RecoverySecurityEvent,
    RecoveryTokenIssue,
    RecoveryTokenPolicy,
    RecoveryTokenProof,
    RecoveryTokenPurpose,
)

NOW = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"
TOKEN_ID = "20000000-0000-4000-8000-000000000002"
ACCOUNT_DIGEST = "sha256:" + "a" * 64
SOURCE_DIGEST = "sha256:" + "b" * 64


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


def _repository(session: _Session) -> PsycopgAccountRecoveryRepository:
    return PsycopgAccountRecoveryRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


def _issue(purpose: RecoveryTokenPurpose = RecoveryTokenPurpose.PASSWORD_RESET):
    return RecoveryTokenIssue(
        token_id=TOKEN_ID,
        purpose=purpose,
        digest=b"d" * 32,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def _event(kind: SecurityEventKind) -> RecoverySecurityEvent:
    return RecoverySecurityEvent(
        event_id=f"30000000-0000-4000-8000-{kind.value[:12].lower():0<12}",
        deployment_id="local-trial",
        kind=kind.value,
        subject_administrator_id=None,
        principal_hint_digest=ACCOUNT_DIGEST,
        source_digest=SOURCE_DIGEST,
        occurred_at=NOW,
    )


def _replacement_hash() -> PasswordHash:
    return PasswordHash(
        algorithm="scrypt",
        parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
        salt=b"s" * 16,
        digest=b"h" * 32,
    )


class PostgreSQLAccountRecoveryTests(unittest.TestCase):
    def test_inspection_is_digest_purpose_state_and_expiry_bound_in_a_closed_transaction(self):
        proof = RecoveryTokenProof(TOKEN_ID, b"d" * 32)
        for row, expected in ((None, None), (("CUSTOMER_USER",), LoginPrincipalKind.CUSTOMER_USER)):
            with self.subTest(row=row):
                session = _Session(responses=[row])
                result = _repository(session).inspect_token_principal(proof, RecoveryTokenPurpose.PASSWORD_RESET, observed_at=NOW)
                self.assertEqual(result, expected)
                self.assertTrue(session.committed and session.closed)
                _, query, parameters = next(event for event in session.events if event[0] == "fetch_one")
                self.assertEqual(parameters, (TOKEN_ID, proof.digest, "PASSWORD_RESET", NOW))
                self.assertIn("state = 'ISSUED' AND expires_at >", query)
                self.assertNotIn("FOR UPDATE", query)

    def test_known_request_uses_separate_throttles_and_replaces_token(self) -> None:
        session = _Session(
            responses=[
                (ADMINISTRATOR_ID, "ACTIVE", 3, 2, False),
                ("ACTIVE", 3, 2),
            ]
        )

        issued = _repository(session).request_password_reset(
            normalized_login_email="operator@example.test",
            account_digest=ACCOUNT_DIGEST,
            source_digest=SOURCE_DIGEST,
            issue=_issue(),
            request_event=_event(SecurityEventKind.PASSWORD_RESET_REQUESTED),
            issue_event=_event(SecurityEventKind.RECOVERY_TOKEN_ISSUED),
            policy=RecoveryTokenPolicy(),
        )

        self.assertTrue(issued)
        throttle_scopes = tuple(
            event[2][0]
            for event in session.events
            if "authentication_throttle" in event[1]
            and event[0] == "execute"
        )
        self.assertEqual(throttle_scopes, ("RESET_ACCOUNT", "RESET_SOURCE"))
        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("FOR UPDATE OF a, p", queries)
        self.assertIn("state = 'REVOKED'", queries)
        self.assertIn("account_recovery_token", queries)
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)

    def test_unknown_request_has_same_event_and_throttles_without_token(self) -> None:
        session = _Session(responses=[(None, None, None, None, False)])

        issued = _repository(session).request_password_reset(
            normalized_login_email="unknown@example.test",
            account_digest=ACCOUNT_DIGEST,
            source_digest=SOURCE_DIGEST,
            issue=_issue(),
            request_event=_event(SecurityEventKind.PASSWORD_RESET_REQUESTED),
            issue_event=_event(SecurityEventKind.RECOVERY_TOKEN_ISSUED),
            policy=RecoveryTokenPolicy(),
        )

        self.assertFalse(issued)
        self.assertEqual(
            sum(
                "INSERT INTO impodo_control.authentication_throttle" in event[1]
                for event in session.events
            ),
            2,
        )
        self.assertEqual(
            sum("platform_security_event" in event[1] for event in session.events),
            1,
        )
        self.assertFalse(
            any("INSERT INTO impodo_control.account_recovery_token" in event[1] for event in session.events)
        )
        self.assertTrue(session.committed)

    def test_consumption_updates_password_security_and_token_in_one_commit(self) -> None:
        session = _Session(
            responses=[(ADMINISTRATOR_ID, "ACTIVE", 3, 2, 3, 2, 1)],
            affected=[1, 1, 1],
        )
        mutation = PasswordResetMutation(
            proof=RecoveryTokenProof(token_id=TOKEN_ID, digest=b"d" * 32),
            replacement_hash=_replacement_hash(),
            source_digest=SOURCE_DIGEST,
            occurred_at=NOW,
        )

        completed = _repository(session).reset_password(
            mutation,
            _event(SecurityEventKind.PASSWORD_RESET_COMPLETED),
        )

        self.assertTrue(completed)
        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("password_revision = password_revision + 1", queries)
        self.assertIn("security_revision = security_revision + 1", queries)
        self.assertIn("state = 'CONSUMED'", queries)
        self.assertIn("token_id <> %s", queries)
        self.assertIn("impodo_control.server_session", queries)
        self.assertIn("revocation_reason = 'PASSWORD_RESET'", queries)
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)

    def test_stale_revision_rolls_back_all_prior_mutations(self) -> None:
        session = _Session(
            responses=[(ADMINISTRATOR_ID, "ACTIVE", 3, 2, 3, 2, 1)],
            affected=[1, 0],
        )
        mutation = PasswordResetMutation(
            proof=RecoveryTokenProof(token_id=TOKEN_ID, digest=b"d" * 32),
            replacement_hash=_replacement_hash(),
            source_digest=SOURCE_DIGEST,
            occurred_at=NOW,
        )

        completed = _repository(session).reset_password(
            mutation,
            _event(SecurityEventKind.PASSWORD_RESET_COMPLETED),
        )

        self.assertFalse(completed)
        self.assertTrue(session.rolled_back)
        self.assertFalse(session.committed)
        self.assertFalse(
            any("platform_security_event" in event[1] for event in session.events)
        )

    def test_activation_is_rejected_until_an_activatable_subject_exists(self) -> None:
        session = _Session()
        command = AuthorizedRecoveryTokenCommand(
            principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
            principal_id=ADMINISTRATOR_ID,
            purpose=RecoveryTokenPurpose.ACTIVATION,
            source_digest=SOURCE_DIGEST,
        )

        with self.assertRaises(AccountRecoverySubjectNotEligibleError):
            _repository(session).issue_authorized(
                command,
                _issue(RecoveryTokenPurpose.ACTIVATION),
                _event(SecurityEventKind.RECOVERY_TOKEN_ISSUED),
            )

        self.assertFalse(session.events)

    def test_default_session_uses_one_runtime_role_transaction(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.platform_recovery.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_account_recovery_session("private-dsn")
            session.close()

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["conninfo"], "private-dsn")
        self.assertFalse(kwargs["autocommit"])
        self.assertEqual(kwargs["application_name"], "impodo-account-recovery")
        self.assertIn(f"role={POSTGRESQL_RUNTIME_ROLE}", kwargs["options"])
        self.assertIn("statement_timeout=10000", kwargs["options"])
        self.assertIn("lock_timeout=5000", kwargs["options"])


if __name__ == "__main__":
    unittest.main()
