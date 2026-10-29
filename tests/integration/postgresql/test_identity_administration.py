"""Verify bounded tenant-scoped PostgreSQL identity administration."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from impodo.adapters.postgresql.identity_administration import (
    POSTGRESQL_IDENTITY_ADMIN_CONNECTION_BUDGET,
    PsycopgIdentityAdministrationRepository,
)
from impodo.domain.identity.administration import (
    AdministrationActor,
    AdministrationEventKind,
    AdministrationEventQuery,
    AdministrationSecurityEvent,
    CustomerCredentialMutation,
    CustomerUserMutationCommand,
    CustomerUserRecord,
    CustomerUserStatus,
    IdentityAdministrationNotFoundError,
    OrganizationListQuery,
    ReplaceOrganizationAdministratorCommand,
)
from impodo.domain.identity.bootstrap import PasswordHash
from impodo.domain.identity.recovery import (
    LoginPrincipalKind,
    RecoveryTokenIssue,
    RecoveryTokenProof,
    RecoveryTokenPurpose,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PLATFORM_ID = "10000000-0000-4000-8000-000000000001"
ADMIN_ID = "20000000-0000-4000-8000-000000000002"
USER_ID = "30000000-0000-4000-8000-000000000003"
EVENT_ID = "40000000-0000-4000-8000-000000000004"
TOKEN_ID = "50000000-0000-4000-8000-000000000005"


class _Session:
    def __init__(
        self,
        responses: list[tuple[object, ...] | None] | None = None,
        affected: list[int] | None = None,
        rows: list[tuple[tuple[object, ...], ...]] | None = None,
    ) -> None:
        self.responses = responses or []
        self.affected = affected or []
        self.rows = rows or []
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

    def fetch_rows(self, query, parameters=()):
        self.events.append(("fetch_rows", query, parameters))
        return self.rows.pop(0)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _repository(session: _Session) -> PsycopgIdentityAdministrationRepository:
    return PsycopgIdentityAdministrationRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


def _platform_actor() -> AdministrationActor:
    return AdministrationActor(
        principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
        principal_id=PLATFORM_ID,
        organization_id=None,
    )


def _organization_actor() -> AdministrationActor:
    return AdministrationActor(
        principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        principal_id=ADMIN_ID,
        organization_id="acme",
    )


def _event(
    kind: AdministrationEventKind,
    actor: AdministrationActor | None,
    *,
    subject_user_id: str | None = None,
) -> AdministrationSecurityEvent:
    return AdministrationSecurityEvent(
        event_id=EVENT_ID,
        deployment_id="local-trial",
        kind=kind,
        organization_id="acme",
        subject_user_id=subject_user_id,
        actor=actor,
        occurred_at=NOW,
    )


def _record(
    kind: LoginPrincipalKind = LoginPrincipalKind.CUSTOMER_USER,
) -> CustomerUserRecord:
    return CustomerUserRecord(
        user_id=USER_ID,
        organization_id="acme",
        normalized_login_email="person@example.test",
        display_name="Person",
        principal_kind=kind,
        status=CustomerUserStatus.PENDING_ACTIVATION,
        security_revision=1,
        optimistic_revision=1,
        created_at=NOW,
    )


def _issue(
    purpose: RecoveryTokenPurpose = RecoveryTokenPurpose.ACTIVATION,
) -> RecoveryTokenIssue:
    return RecoveryTokenIssue(
        token_id=TOKEN_ID,
        purpose=purpose,
        digest=b"d" * 32,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def _customer_row(
    *,
    status: CustomerUserStatus = CustomerUserStatus.ACTIVE,
    kind: LoginPrincipalKind = LoginPrincipalKind.CUSTOMER_USER,
    security_revision: int = 1,
    optimistic_revision: int = 1,
) -> tuple[object, ...]:
    return (
        USER_ID,
        "acme",
        "person@example.test",
        "Person",
        kind.value,
        status.value,
        security_revision,
        optimistic_revision,
    )


class PostgreSQLIdentityAdministrationTests(unittest.TestCase):
    def test_organization_page_is_authorized_and_uses_a_stable_keyset(self) -> None:
        session = _Session(
            responses=[("ACTIVE",)],
            rows=[
                (
                    ("acme", "Acme", "ACTIVE", 1, 2),
                    ("beta", "Beta", "SUSPENDED", 3, 4),
                    ("gamma", "Gamma", "ACTIVE", 1, 1),
                )
            ],
        )

        page = _repository(session).list_organizations(
            OrganizationListQuery(
                actor=_platform_actor(),
                page_size=2,
                after_display_name="Able",
                after_organization_id="able",
            )
        )

        self.assertEqual(
            tuple(item.organization_id for item in page.items),
            ("acme", "beta"),
        )
        self.assertEqual(page.next_after_display_name, "Beta")
        self.assertEqual(page.next_after_organization_id, "beta")
        query_event = next(
            event for event in session.events if event[0] == "fetch_rows"
        )
        self.assertIn("(display_name, organization_id) >", query_event[1])
        self.assertIn("ORDER BY display_name ASC, organization_id ASC", query_event[1])
        self.assertEqual(query_event[2], ("Able", "Able", "able", 3))
        self.assertTrue(session.committed)

    def test_initial_administrator_assignment_advances_org_and_records_assignment(
        self,
    ) -> None:
        session = _Session(
            responses=[("ACTIVE",), ("ACTIVE", 1), None],
            affected=[1],
        )
        command = ReplaceOrganizationAdministratorCommand(
            actor=_platform_actor(),
            organization_id="acme",
            login_email="admin@example.test",
            display_name="Acme Administrator",
            expected_organization_revision=1,
        )

        user = _repository(session).replace_organization_administrator(
            command,
            _record(LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR),
            _issue(),
            _event(
                AdministrationEventKind.ORGANIZATION_ADMINISTRATOR_REPLACED,
                _platform_actor(),
                subject_user_id=USER_ID,
            ),
        )

        self.assertEqual(user.principal_kind, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR)
        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("organization_administrator_assignment", queries)
        self.assertIn("optimistic_revision = optimistic_revision + 1", queries)
        event_parameters = next(
            event[2]
            for event in session.events
            if "identity_administration_event" in event[1]
        )
        self.assertEqual(
            event_parameters[2],
            AdministrationEventKind.ORGANIZATION_ADMINISTRATOR_ASSIGNED.value,
        )
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)

    def test_unknown_scoped_user_is_not_found_before_any_mutation(self) -> None:
        session = _Session(
            responses=[("ACTIVE", "ACTIVE"), ("ACTIVE", 2), None],
        )
        command = CustomerUserMutationCommand(
            actor=_organization_actor(),
            organization_id="acme",
            user_id=USER_ID,
            expected_revision=1,
        )

        with self.assertRaises(IdentityAdministrationNotFoundError):
            _repository(session).set_customer_user_status(
                command,
                CustomerUserStatus.SUSPENDED,
                _event(
                    AdministrationEventKind.CUSTOMER_USER_SUSPENDED,
                    _organization_actor(),
                    subject_user_id=USER_ID,
                ),
                occurred_at=NOW,
            )

        self.assertTrue(session.rolled_back)
        self.assertFalse(session.committed)
        self.assertFalse(any(event[0] == "execute_affected" for event in session.events))

    def test_activation_consumes_token_and_activates_in_one_transaction(self) -> None:
        session = _Session(
            responses=[
                (*_customer_row(status=CustomerUserStatus.PENDING_ACTIVATION), 1),
                _customer_row(status=CustomerUserStatus.ACTIVE, optimistic_revision=2),
            ],
            affected=[1],
        )
        mutation = CustomerCredentialMutation(
            proof=RecoveryTokenProof(token_id=TOKEN_ID, digest=b"d" * 32),
            password_hash=PasswordHash(
                algorithm="scrypt",
                parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
                salt=b"s" * 16,
                digest=b"h" * 32,
            ),
            occurred_at=NOW,
        )

        user = _repository(session).activate_customer_user(
            mutation,
            _event(AdministrationEventKind.CUSTOMER_USER_ACTIVATED, None),
        )

        assert user is not None
        self.assertEqual(user.status, CustomerUserStatus.ACTIVE)
        queries = "\n".join(event[1] for event in session.events)
        self.assertIn("customer_user_password_identity", queries)
        self.assertIn("state = 'CONSUMED'", queries)
        self.assertIn("u.principal_kind = t.principal_kind", queries)
        self.assertIn("principal_kind IN", queries)
        self.assertTrue(session.committed)

    def test_status_predicate_contains_tenant_user_and_expected_revision(self) -> None:
        session = _Session(
            responses=[
                ("ACTIVE", "ACTIVE"),
                ("ACTIVE", 2),
                _customer_row(),
                _customer_row(
                    status=CustomerUserStatus.SUSPENDED,
                    security_revision=2,
                    optimistic_revision=2,
                ),
            ]
        )
        command = CustomerUserMutationCommand(
            actor=_organization_actor(),
            organization_id="acme",
            user_id=USER_ID,
            expected_revision=1,
        )

        _repository(session).set_customer_user_status(
            command,
            CustomerUserStatus.SUSPENDED,
            _event(
                AdministrationEventKind.CUSTOMER_USER_SUSPENDED,
                _organization_actor(),
                subject_user_id=USER_ID,
            ),
            occurred_at=NOW,
        )

        status_query = next(
            event[1]
            for event in session.events
            if "SET status = %s" in event[1]
        )
        self.assertIn("organization_id = %s AND user_id = %s", status_query)
        self.assertIn("optimistic_revision = %s", status_query)
        self.assertIn("security_revision = security_revision + 1", status_query)
        self.assertIn("server_session", "\n".join(event[1] for event in session.events))

    def test_user_event_page_filters_inside_the_organization_predicate(self) -> None:
        session = _Session(
            responses=[("ACTIVE", "ACTIVE")],
            rows=[
                (
                    (
                        EVENT_ID,
                        AdministrationEventKind.CUSTOMER_USER_ACTIVATED.value,
                        "acme",
                        USER_ID,
                        None,
                        None,
                        NOW,
                    ),
                )
            ],
        )

        page = _repository(session).list_security_events(
            AdministrationEventQuery(
                actor=_organization_actor(),
                organization_id="acme",
                user_id=USER_ID,
            )
        )

        self.assertEqual(tuple(item.subject_user_id for item in page.items), (USER_ID,))
        query_event = next(
            event for event in session.events if event[0] == "fetch_rows"
        )
        self.assertIn("WHERE organization_id = %s", query_event[1])
        self.assertIn("subject_user_id = %s", query_event[1])
        self.assertEqual(query_event[2][:3], ("acme", USER_ID, USER_ID))

    def test_repository_contract_uses_one_connection_per_operation(self) -> None:
        self.assertEqual(POSTGRESQL_IDENTITY_ADMIN_CONNECTION_BUDGET, 1)


if __name__ == "__main__":
    unittest.main()
