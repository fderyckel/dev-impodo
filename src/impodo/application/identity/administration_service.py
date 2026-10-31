"""Orchestrate tenant-scoped Organization and customer-account administration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.identity.administration_ports import (
    IdentityAdministrationRepository,
)
from impodo.application.identity.authentication_ports import (
    PasswordAuthenticationPolicy,
)
from impodo.application.identity.recovery_ports import RecoveryTokenFactory
from impodo.domain.identity.administration import (
    ActivateCustomerUserCommand,
    AdministrationActor,
    AdministrationEventKind,
    AdministrationEventPage,
    AdministrationEventQuery,
    AdministrationSecurityEvent,
    CreateCustomerUserCommand,
    CreateOrganizationCommand,
    CustomerCredentialMutation,
    CustomerUserDetailQuery,
    CustomerUserListQuery,
    CustomerUserMutationCommand,
    CustomerUserPage,
    CustomerUserRecord,
    CustomerUserStatus,
    CustomerUserView,
    IdentityAdministrationInputError,
    OrganizationAdministrationView,
    OrganizationDetailQuery,
    OrganizationListQuery,
    OrganizationMutationCommand,
    OrganizationPage,
    OrganizationSessionPage,
    OrganizationSessionQuery,
    OrganizationStatus,
    OrganizationView,
    ProvisionedCustomerUserResult,
    RenameOrganizationCommand,
    ReplaceOrganizationAdministratorCommand,
    ReviseOrganizationPolicyCommand,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.bootstrap import (
    PlatformAdministratorBootstrapInputError,
    normalize_login_email,
    validate_bootstrap_password,
)
from impodo.domain.identity.recovery import (
    PASSWORD_RESET_REJECTED_MESSAGE,
    AccountRecoveryInputError,
    AccountRecoveryRejectedError,
    AuthorizedRecoveryTokenResult,
    LoginPrincipalKind,
    PasswordResetCommand,
    RecoveryTokenIssue,
    RecoveryTokenPolicy,
    RecoveryTokenPurpose,
)


class IdentityAdministrationService:
    """Keep secret derivation outside short tenant-scoped transactions."""

    def __init__(
        self,
        repository: IdentityAdministrationRepository,
        token_factory: RecoveryTokenFactory,
        password_policy: PasswordAuthenticationPolicy,
        *,
        token_policy: RecoveryTokenPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._token_factory = token_factory
        self._password_policy = password_policy
        self._token_policy = token_policy or RecoveryTokenPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def create_organization(
        self,
        command: CreateOrganizationCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView:
        """Create one empty Organization without granting customer-data access."""

        occurred_at = self._aware_now()
        return self._repository.create_organization(
            organization_id=command.organization_id,
            display_name=command.display_name,
            occurred_at=occurred_at,
            event=self._event(
                deployment_id,
                AdministrationEventKind.ORGANIZATION_CREATED,
                command.organization_id,
                None,
                command.actor,
                occurred_at,
            ),
        )

    def list_organizations(
        self,
        query: OrganizationListQuery,
    ) -> OrganizationPage:
        """Return one deterministic Platform Administrator Organization page."""

        return self._repository.list_organizations(query)

    def rename_organization(
        self,
        command: RenameOrganizationCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView:
        """Rename one exact Organization under optimistic concurrency."""

        occurred_at = self._aware_now()
        return self._repository.rename_organization(
            command,
            self._event(
                deployment_id,
                AdministrationEventKind.ORGANIZATION_RENAMED,
                command.mutation.organization_id,
                None,
                command.mutation.actor,
                occurred_at,
            ),
            occurred_at=occurred_at,
        )

    def get_organization(self, query: OrganizationDetailQuery) -> OrganizationAdministrationView:
        """Read one exact Organization and its assigned administrator."""

        return self._repository.get_organization(query)

    def reissue_customer_activation(
        self, command: CustomerUserMutationCommand, *, deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult:
        """Replace a pending ordinary user's expired or lost activation link."""

        return self._issue_administration_token(command, RecoveryTokenPurpose.ACTIVATION, deployment_id)

    def issue_administrator_token(
        self, command: OrganizationMutationCommand, purpose: RecoveryTokenPurpose,
        *, deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult:
        """Issue a link for the exact current administrator under platform authority."""

        return self._issue_administration_token(command, purpose, deployment_id)

    def _issue_administration_token(
        self, command: CustomerUserMutationCommand | OrganizationMutationCommand,
        purpose: RecoveryTokenPurpose, deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult:
        occurred_at = self._aware_now()
        material = self._token_factory.create(purpose)
        issue = self._issue(material.token_id, material.digest, occurred_at, purpose)
        event = self._event(
            deployment_id,
            AdministrationEventKind.CUSTOMER_ACTIVATION_TOKEN_REISSUED
            if purpose is RecoveryTokenPurpose.ACTIVATION
            else AdministrationEventKind.CUSTOMER_RESET_TOKEN_ISSUED,
            command.organization_id,
            command.user_id if isinstance(command, CustomerUserMutationCommand) else None,
            command.actor, occurred_at,
        )
        if isinstance(command, CustomerUserMutationCommand):
            self._repository.reissue_customer_activation(command, issue, event)
        else:
            self._repository.issue_administrator_token(command, issue, event)
        return AuthorizedRecoveryTokenResult(purpose=purpose, token=material.token, expires_at=issue.expires_at)

    def suspend_organization(
        self,
        command: OrganizationMutationCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView:
        """Suspend one Organization and revoke all of its active sessions."""

        return self._set_organization_status(
            command,
            OrganizationStatus.SUSPENDED,
            AdministrationEventKind.ORGANIZATION_SUSPENDED,
            deployment_id,
        )

    def reactivate_organization(
        self,
        command: OrganizationMutationCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView:
        """Reactivate one Organization without restoring revoked sessions."""

        return self._set_organization_status(
            command,
            OrganizationStatus.ACTIVE,
            AdministrationEventKind.ORGANIZATION_REACTIVATED,
            deployment_id,
        )

    def revise_organization_policy(
        self,
        command: ReviseOrganizationPolicyCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView:
        """Advance policy and revoke sessions bound to the previous revision."""

        occurred_at = self._aware_now()
        return self._repository.revise_organization_policy(
            command,
            self._event(
                deployment_id,
                AdministrationEventKind.ORGANIZATION_POLICY_REVISED,
                command.mutation.organization_id,
                None,
                command.mutation.actor,
                occurred_at,
            ),
            occurred_at=occurred_at,
        )

    def create_customer_user(
        self,
        command: CreateCustomerUserCommand,
        *,
        deployment_id: str,
    ) -> ProvisionedCustomerUserResult:
        """Create one pending user and disclose its activation token after commit."""

        occurred_at = self._aware_now()
        record, issue, token = self._pending_customer(
            organization_id=command.organization_id,
            login_email=command.login_email,
            display_name=command.display_name,
            principal_kind=command.principal_kind,
            occurred_at=occurred_at,
        )
        user = self._repository.create_customer_user(
            record,
            issue,
            self._event(
                deployment_id,
                AdministrationEventKind.CUSTOMER_USER_CREATED,
                command.organization_id,
                record.user_id,
                command.actor,
                occurred_at,
            ),
        )
        return ProvisionedCustomerUserResult(
            user=user,
            activation_token=token,
            expires_at=issue.expires_at,
        )

    def list_customer_users(
        self,
        query: CustomerUserListQuery,
    ) -> CustomerUserPage:
        """Return one deterministic page inside the actor's Organization."""

        return self._repository.list_customer_users(query)

    def get_customer_user(
        self,
        query: CustomerUserDetailQuery,
    ) -> CustomerUserView:
        """Return one exact tenant-bound customer account."""

        return self._repository.get_customer_user(query)

    def replace_organization_administrator(
        self,
        command: ReplaceOrganizationAdministratorCommand,
        *,
        deployment_id: str,
    ) -> ProvisionedCustomerUserResult:
        """Replace the assignment and disclose only the new activation token."""

        occurred_at = self._aware_now()
        record, issue, token = self._pending_customer(
            organization_id=command.organization_id,
            login_email=command.login_email,
            display_name=command.display_name,
            principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
            occurred_at=occurred_at,
        )
        user = self._repository.replace_organization_administrator(
            command,
            record,
            issue,
            self._event(
                deployment_id,
                AdministrationEventKind.ORGANIZATION_ADMINISTRATOR_REPLACED,
                command.organization_id,
                record.user_id,
                command.actor,
                occurred_at,
            ),
        )
        return ProvisionedCustomerUserResult(
            user=user,
            activation_token=token,
            expires_at=issue.expires_at,
        )

    def activate_customer_user(
        self,
        command: ActivateCustomerUserCommand,
        *,
        deployment_id: str,
    ) -> CustomerUserView:
        """Consume activation with the initial password and account transition."""

        occurred_at = self._aware_now()
        try:
            proof = self._token_factory.prove(
                command.token,
                RecoveryTokenPurpose.ACTIVATION,
            )
            password = validate_bootstrap_password(command.password)
        except (AccountRecoveryInputError, PlatformAdministratorBootstrapInputError):
            raise AccountRecoveryRejectedError(
                PASSWORD_RESET_REJECTED_MESSAGE
            ) from None
        password_hash = self._password_policy.hash(password)
        user = self._repository.activate_customer_user(
            CustomerCredentialMutation(
                proof=proof,
                password_hash=password_hash,
                occurred_at=occurred_at,
            ),
            self._event(
                deployment_id,
                AdministrationEventKind.CUSTOMER_USER_ACTIVATED,
                None,
                None,
                None,
                occurred_at,
            ),
        )
        if user is None:
            raise AccountRecoveryRejectedError(PASSWORD_RESET_REJECTED_MESSAGE)
        return user

    def suspend_customer_user(
        self,
        command: CustomerUserMutationCommand,
        *,
        deployment_id: str,
    ) -> CustomerUserView:
        """Suspend one customer account and revoke all of its sessions."""

        return self._set_customer_status(
            command,
            CustomerUserStatus.SUSPENDED,
            AdministrationEventKind.CUSTOMER_USER_SUSPENDED,
            deployment_id,
        )

    def reactivate_customer_user(
        self,
        command: CustomerUserMutationCommand,
        *,
        deployment_id: str,
    ) -> CustomerUserView:
        """Reactivate one suspended account without restoring sessions."""

        return self._set_customer_status(
            command,
            CustomerUserStatus.ACTIVE,
            AdministrationEventKind.CUSTOMER_USER_REACTIVATED,
            deployment_id,
        )

    def deactivate_customer_user(
        self,
        command: CustomerUserMutationCommand,
        *,
        deployment_id: str,
    ) -> CustomerUserView:
        """Irreversibly deactivate one customer account and revoke sessions."""

        return self._set_customer_status(
            command,
            CustomerUserStatus.DEACTIVATED,
            AdministrationEventKind.CUSTOMER_USER_DEACTIVATED,
            deployment_id,
        )

    def issue_customer_reset(
        self,
        command: CustomerUserMutationCommand,
        *,
        deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult:
        """Issue one exact customer reset token and disclose it after commit."""

        occurred_at = self._aware_now()
        material = self._token_factory.create(RecoveryTokenPurpose.PASSWORD_RESET)
        issue = self._issue(material.token_id, material.digest, occurred_at, RecoveryTokenPurpose.PASSWORD_RESET)
        self._repository.issue_customer_reset(
            command,
            issue,
            self._event(
                deployment_id,
                AdministrationEventKind.CUSTOMER_RESET_TOKEN_ISSUED,
                command.organization_id,
                command.user_id,
                command.actor,
                occurred_at,
            ),
        )
        return AuthorizedRecoveryTokenResult(
            purpose=RecoveryTokenPurpose.PASSWORD_RESET,
            token=material.token,
            expires_at=issue.expires_at,
        )

    def reset_customer_password(
        self,
        command: PasswordResetCommand,
        *,
        deployment_id: str,
    ) -> CustomerUserView:
        """Consume one customer reset token with password and session revocation."""

        occurred_at = self._aware_now()
        try:
            proof = self._token_factory.prove(
                command.token,
                RecoveryTokenPurpose.PASSWORD_RESET,
            )
            password = validate_bootstrap_password(command.new_password)
        except (AccountRecoveryInputError, PlatformAdministratorBootstrapInputError):
            raise AccountRecoveryRejectedError(
                PASSWORD_RESET_REJECTED_MESSAGE
            ) from None
        password_hash = self._password_policy.hash(password)
        user = self._repository.reset_customer_password(
            CustomerCredentialMutation(
                proof=proof,
                password_hash=password_hash,
                occurred_at=occurred_at,
            ),
            self._event(
                deployment_id,
                AdministrationEventKind.CUSTOMER_PASSWORD_RESET,
                None,
                None,
                None,
                occurred_at,
            ),
        )
        if user is None:
            raise AccountRecoveryRejectedError(PASSWORD_RESET_REJECTED_MESSAGE)
        return user

    def list_security_events(
        self,
        query: AdministrationEventQuery,
    ) -> AdministrationEventPage:
        """Return one tenant-scoped newest-first event page."""

        return self._repository.list_security_events(query)

    def list_sessions(
        self,
        query: OrganizationSessionQuery,
    ) -> OrganizationSessionPage:
        """Return one tenant-scoped newest-first session page."""

        return self._repository.list_sessions(query)

    def revoke_session(
        self,
        command: RevokeOrganizationSessionCommand,
        *,
        deployment_id: str,
    ) -> None:
        """Revoke one exact Organization session without cross-tenant disclosure."""

        occurred_at = self._aware_now()
        self._repository.revoke_session(
            command,
            self._event(
                deployment_id,
                AdministrationEventKind.CUSTOMER_SESSION_REVOKED,
                command.organization_id,
                None,
                command.actor,
                occurred_at,
            ),
            revoked_at=occurred_at,
        )

    def _set_organization_status(
        self,
        command: OrganizationMutationCommand,
        status: OrganizationStatus,
        kind: AdministrationEventKind,
        deployment_id: str,
    ) -> OrganizationView:
        occurred_at = self._aware_now()
        return self._repository.set_organization_status(
            command,
            status,
            self._event(
                deployment_id,
                kind,
                command.organization_id,
                None,
                command.actor,
                occurred_at,
            ),
            occurred_at=occurred_at,
        )

    def _set_customer_status(
        self,
        command: CustomerUserMutationCommand,
        status: CustomerUserStatus,
        kind: AdministrationEventKind,
        deployment_id: str,
    ) -> CustomerUserView:
        occurred_at = self._aware_now()
        return self._repository.set_customer_user_status(
            command,
            status,
            self._event(
                deployment_id,
                kind,
                command.organization_id,
                command.user_id,
                command.actor,
                occurred_at,
            ),
            occurred_at=occurred_at,
        )

    def _pending_customer(
        self,
        *,
        organization_id: str,
        login_email: str,
        display_name: str,
        principal_kind: LoginPrincipalKind,
        occurred_at: datetime,
    ) -> tuple[CustomerUserRecord, RecoveryTokenIssue, str]:
        try:
            normalized_email = normalize_login_email(login_email)
        except PlatformAdministratorBootstrapInputError as error:
            raise IdentityAdministrationInputError("Login email is invalid") from error
        material = self._token_factory.create(RecoveryTokenPurpose.ACTIVATION)
        return (
            CustomerUserRecord(
                user_id=self._identifier_factory(),
                organization_id=organization_id,
                normalized_login_email=normalized_email,
                display_name=display_name,
                principal_kind=principal_kind,
                status=CustomerUserStatus.PENDING_ACTIVATION,
                security_revision=1,
                optimistic_revision=1,
                created_at=occurred_at,
            ),
            self._issue(
                material.token_id,
                material.digest,
                occurred_at,
                RecoveryTokenPurpose.ACTIVATION,
            ),
            material.token,
        )

    def _issue(
        self,
        token_id: str,
        digest: bytes,
        occurred_at: datetime,
        purpose: RecoveryTokenPurpose,
    ) -> RecoveryTokenIssue:
        return RecoveryTokenIssue(
            token_id=token_id,
            purpose=purpose,
            digest=digest,
            issued_at=occurred_at,
            expires_at=occurred_at + self._token_policy.lifetime_for(purpose),
        )

    def _event(
        self,
        deployment_id: str,
        kind: AdministrationEventKind,
        organization_id: str | None,
        subject_user_id: str | None,
        actor: AdministrationActor | None,
        occurred_at: datetime,
    ) -> AdministrationSecurityEvent:
        return AdministrationSecurityEvent(
            event_id=self._identifier_factory(),
            deployment_id=deployment_id,
            kind=kind,
            organization_id=organization_id,
            subject_user_id=subject_user_id,
            actor=actor,
            occurred_at=occurred_at,
        )

    def _aware_now(self) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("identity administration clock must be aware")
        return observed_at


__all__ = ["IdentityAdministrationService"]
