"""Declare transactional Organization administration persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.identity.administration import (
    AdministrationEventPage,
    AdministrationEventQuery,
    AdministrationSecurityEvent,
    CustomerCredentialMutation,
    CustomerUserMutationCommand,
    CustomerUserRecord,
    CustomerUserStatus,
    CustomerUserView,
    OrganizationListQuery,
    OrganizationMutationCommand,
    OrganizationPage,
    OrganizationSessionPage,
    OrganizationSessionQuery,
    OrganizationStatus,
    OrganizationView,
    RenameOrganizationCommand,
    ReplaceOrganizationAdministratorCommand,
    ReviseOrganizationPolicyCommand,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.recovery import RecoveryTokenIssue


class IdentityAdministrationRepository(Protocol):
    """Own one bounded transaction for each administration operation."""

    def create_organization(
        self,
        *,
        organization_id: str,
        display_name: str,
        occurred_at: datetime,
        event: AdministrationSecurityEvent,
    ) -> OrganizationView: ...

    def list_organizations(
        self,
        query: OrganizationListQuery,
    ) -> OrganizationPage: ...

    def rename_organization(
        self,
        command: RenameOrganizationCommand,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView: ...

    def set_organization_status(
        self,
        command: OrganizationMutationCommand,
        status: OrganizationStatus,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView: ...

    def revise_organization_policy(
        self,
        command: ReviseOrganizationPolicyCommand,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> OrganizationView: ...

    def create_customer_user(
        self,
        record: CustomerUserRecord,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView: ...

    def replace_organization_administrator(
        self,
        command: ReplaceOrganizationAdministratorCommand,
        record: CustomerUserRecord,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView: ...

    def activate_customer_user(
        self,
        mutation: CustomerCredentialMutation,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView | None: ...

    def set_customer_user_status(
        self,
        command: CustomerUserMutationCommand,
        status: CustomerUserStatus,
        event: AdministrationSecurityEvent,
        *,
        occurred_at: datetime,
    ) -> CustomerUserView: ...

    def issue_customer_reset(
        self,
        command: CustomerUserMutationCommand,
        issue: RecoveryTokenIssue,
        event: AdministrationSecurityEvent,
    ) -> None: ...

    def reset_customer_password(
        self,
        mutation: CustomerCredentialMutation,
        event: AdministrationSecurityEvent,
    ) -> CustomerUserView | None: ...

    def list_security_events(
        self,
        query: AdministrationEventQuery,
    ) -> AdministrationEventPage: ...

    def list_sessions(
        self,
        query: OrganizationSessionQuery,
    ) -> OrganizationSessionPage: ...

    def revoke_session(
        self,
        command: RevokeOrganizationSessionCommand,
        event: AdministrationSecurityEvent,
        *,
        revoked_at: datetime,
    ) -> None: ...


__all__ = ["IdentityAdministrationRepository"]
