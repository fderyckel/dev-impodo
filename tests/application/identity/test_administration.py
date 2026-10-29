"""Verify tenant-safe Organization and customer-account orchestration."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.adapters.identity.secure_recovery_tokens import SecureRecoveryTokenFactory
from impodo.application.identity.administration_service import (
    IdentityAdministrationService,
)
from impodo.domain.identity.administration import (
    ActivateCustomerUserCommand,
    AdministrationActor,
    AdministrationEventQuery,
    CreateCustomerUserCommand,
    CreateOrganizationCommand,
    CustomerUserRecord,
    CustomerUserStatus,
    CustomerUserView,
    IdentityAdministrationInputError,
    IdentityAdministrationNotFoundError,
    OrganizationListQuery,
    OrganizationPage,
    ReplaceOrganizationAdministratorCommand,
)
from impodo.domain.identity.bootstrap import PasswordHash
from impodo.domain.identity.recovery import (
    PASSWORD_RESET_REJECTED_MESSAGE,
    AccountRecoveryRejectedError,
    LoginPrincipalKind,
    RecoveryTokenPurpose,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PLATFORM_ID = "10000000-0000-4000-8000-000000000001"
ADMIN_ID = "20000000-0000-4000-8000-000000000002"
OTHER_ADMIN_ID = "30000000-0000-4000-8000-000000000003"
USER_ID = "40000000-0000-4000-8000-000000000004"
EVENT_ID = "50000000-0000-4000-8000-000000000005"


class _PasswordHasher:
    def __init__(self) -> None:
        self.passwords: list[str] = []

    def hash(self, password: str) -> PasswordHash:
        self.passwords.append(password)
        return PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=b"s" * 16,
            digest=b"h" * 32,
        )

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        return False

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        return False


class _Repository:
    def __init__(self) -> None:
        self.created: tuple[CustomerUserRecord, object, object] | None = None
        self.replaced: tuple[object, CustomerUserRecord, object, object] | None = None
        self.activation = None
        self.activation_result: CustomerUserView | None = None
        self.organization_page = OrganizationPage(
            items=(),
            next_after_display_name=None,
            next_after_organization_id=None,
        )

    def create_organization(self, **kwargs):
        return kwargs

    def list_organizations(self, query):
        self.organization_query = query
        return self.organization_page

    def create_customer_user(self, record, issue, event):
        self.created = (record, issue, event)
        return _view(record)

    def replace_organization_administrator(self, command, record, issue, event):
        self.replaced = (command, record, issue, event)
        return _view(record)

    def activate_customer_user(self, mutation, event):
        self.activation = (mutation, event)
        return self.activation_result


def _platform_actor() -> AdministrationActor:
    return AdministrationActor(
        principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
        principal_id=PLATFORM_ID,
        organization_id=None,
    )


def _organization_actor(
    organization_id: str = "acme",
    principal_id: str = ADMIN_ID,
) -> AdministrationActor:
    return AdministrationActor(
        principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        principal_id=principal_id,
        organization_id=organization_id,
    )


def _service(
    repository: _Repository,
    password_hasher: _PasswordHasher | None = None,
) -> IdentityAdministrationService:
    identifiers = iter((USER_ID, EVENT_ID, OTHER_ADMIN_ID, EVENT_ID))
    return IdentityAdministrationService(
        repository,
        SecureRecoveryTokenFactory(),
        password_hasher or _PasswordHasher(),
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
    )


def _view(record: CustomerUserRecord) -> CustomerUserView:
    return CustomerUserView(
        user_id=record.user_id,
        organization_id=record.organization_id,
        normalized_login_email=record.normalized_login_email,
        display_name=record.display_name,
        principal_kind=record.principal_kind,
        status=record.status,
        security_revision=record.security_revision,
        optimistic_revision=record.optimistic_revision,
    )


class IdentityAdministrationServiceTests(unittest.TestCase):
    def test_customer_operations_require_the_exact_organization_administrator(
        self,
    ) -> None:
        with self.assertRaises(IdentityAdministrationInputError):
            CreateCustomerUserCommand(
                actor=_platform_actor(),
                organization_id="acme",
                login_email="person@example.test",
                display_name="Person",
            )
        with self.assertRaises(IdentityAdministrationNotFoundError):
            CreateCustomerUserCommand(
                actor=_organization_actor("other", OTHER_ADMIN_ID),
                organization_id="acme",
                login_email="person@example.test",
                display_name="Person",
            )
        with self.assertRaises(IdentityAdministrationInputError):
            AdministrationEventQuery(
                actor=_platform_actor(),
                organization_id="acme",
            )
        with self.assertRaises(IdentityAdministrationInputError):
            AdministrationEventQuery(
                actor=_organization_actor(),
                organization_id="acme",
                user_id="not-a-user-id",
            )

    def test_customer_creation_discloses_only_the_post_commit_activation_token(
        self,
    ) -> None:
        repository = _Repository()
        result = _service(repository).create_customer_user(
            CreateCustomerUserCommand(
                actor=_organization_actor(),
                organization_id="acme",
                login_email=" Person@Example.Test ",
                display_name="Person",
            ),
            deployment_id="local-trial",
        )

        assert repository.created is not None
        record, issue, event = repository.created
        self.assertEqual(record.normalized_login_email, "person@example.test")
        self.assertEqual(record.principal_kind, LoginPrincipalKind.CUSTOMER_USER)
        self.assertEqual(record.status, CustomerUserStatus.PENDING_ACTIVATION)
        self.assertNotIn(result.activation_token, repr(result))
        self.assertNotIn(result.activation_token, repr(issue))
        self.assertNotIn(result.activation_token, repr(event))

    def test_platform_administrator_assigns_the_initial_organization_admin(
        self,
    ) -> None:
        repository = _Repository()
        result = _service(repository).replace_organization_administrator(
            ReplaceOrganizationAdministratorCommand(
                actor=_platform_actor(),
                organization_id="acme",
                login_email="admin@example.test",
                display_name="Acme Administrator",
                expected_organization_revision=1,
            ),
            deployment_id="local-trial",
        )

        assert repository.replaced is not None
        _, record, issue, event = repository.replaced
        self.assertEqual(
            result.user.principal_kind,
            LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        )
        self.assertNotIn(result.activation_token, repr(issue))
        self.assertNotIn(result.activation_token, repr(event))
        self.assertEqual(record.organization_id, "acme")

    def test_activation_hashes_before_one_generic_rejection(self) -> None:
        repository = _Repository()
        hasher = _PasswordHasher()
        token = SecureRecoveryTokenFactory().create(
            purpose=RecoveryTokenPurpose.ACTIVATION
        ).token

        with self.assertRaisesRegex(
            AccountRecoveryRejectedError,
            f"^{PASSWORD_RESET_REJECTED_MESSAGE}$",
        ):
            _service(repository, hasher).activate_customer_user(
                ActivateCustomerUserCommand(
                    token=token,
                    password="fictional activation password",
                    source_digest="source-not-persisted",
                ),
                deployment_id="local-trial",
            )

        self.assertEqual(hasher.passwords, ["fictional activation password"])
        self.assertIsNotNone(repository.activation)
        self.assertNotIn("fictional activation password", repr(repository.activation))

    def test_platform_organization_creation_carries_no_customer_payload(self) -> None:
        repository = _Repository()
        result = _service(repository).create_organization(
            CreateOrganizationCommand(
                actor=_platform_actor(),
                organization_id="acme",
                display_name="Acme",
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(result["organization_id"], "acme")
        self.assertNotIn("login_email", result)

    def test_platform_organization_listing_preserves_the_keyset_cursor(self) -> None:
        repository = _Repository()
        query = OrganizationListQuery(
            actor=_platform_actor(),
            page_size=25,
            after_display_name="Acme",
            after_organization_id="acme",
        )

        page = _service(repository).list_organizations(query)

        self.assertIs(page, repository.organization_page)
        self.assertIs(repository.organization_query, query)


if __name__ == "__main__":
    unittest.main()
