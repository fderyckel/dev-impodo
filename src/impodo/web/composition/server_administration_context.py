"""Declare server browser ports and shared request authorization boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from impodo.domain.identity.administration import (
    ActivateCustomerUserCommand,
    AdministrationActor,
    AdministrationEventPage,
    AdministrationEventQuery,
    CreateCustomerUserCommand,
    CreateOrganizationCommand,
    CustomerUserDetailQuery,
    CustomerUserListQuery,
    CustomerUserMutationCommand,
    CustomerUserPage,
    CustomerUserView,
    IdentityAdministrationConflictError,
    IdentityAdministrationInputError,
    IdentityAdministrationNotFoundError,
    IdentityAdministrationStorageError,
    IdentityAdministrationUnavailableError,
    OrganizationAdministrationView,
    OrganizationDetailQuery,
    OrganizationListQuery,
    OrganizationMutationCommand,
    OrganizationPage,
    OrganizationSessionPage,
    OrganizationSessionQuery,
    OrganizationView,
    ProvisionedCustomerUserResult,
    RenameOrganizationCommand,
    ReplaceOrganizationAdministratorCommand,
    ReviseOrganizationPolicyCommand,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.recovery import (
    AccountRecoveryRejectedError,
    AccountRecoveryStorageError,
    AccountRecoverySubjectNotEligibleError,
    AccountRecoveryUnavailableError,
    AuthorizedRecoveryTokenCommand,
    AuthorizedRecoveryTokenResult,
    LoginPrincipalKind,
    PasswordResetCommand,
    PasswordResetRequestCommand,
    PasswordResetRequestResult,
    PasswordResetResult,
    RecoveryTokenPurpose,
)
from impodo.domain.identity.server_authentication import (
    ServerAuthenticationCommand,
    ServerAuthenticationResult,
)
from impodo.domain.identity.sessions import (
    AccessServerSessionCommand,
    CreateServerSessionCommand,
    CurrentSessionView,
    RevokeServerSessionCommand,
    ServerSessionCredentials,
    ServerSessionRejectedError,
    ServerSessionStorageError,
    ServerSessionUnavailableError,
)
from impodo.web.composition.server_account_admission import (
    ServerAccountAdmission,
    ServerAccountBusyError,
)
from impodo.web.composition.server_session_http import (
    SERVER_SESSION_COOKIE_NAME,
    clear_server_session_cookie,
)


class ServerRoleForbiddenError(RuntimeError):
    """Deny a role boundary while keeping the valid session."""


class ServerAuthenticator(Protocol):
    """Authenticate one global server identity."""

    def authenticate(
        self, command: ServerAuthenticationCommand, *, deployment_id: str
    ) -> ServerAuthenticationResult: ...


class ServerSessionManager(Protocol):
    """Create, resolve, and revoke opaque server sessions."""

    def create(
        self, command: CreateServerSessionCommand, *, deployment_id: str
    ) -> ServerSessionCredentials: ...
    def access(self, command: AccessServerSessionCommand) -> CurrentSessionView: ...
    def revoke(
        self, command: RevokeServerSessionCommand, *, deployment_id: str
    ) -> None: ...


class ServerIdentityAdministration(Protocol):
    """Expose the bounded administration service without adapter construction."""

    def list_organizations(self, query: OrganizationListQuery) -> OrganizationPage: ...
    def get_organization(
        self, query: OrganizationDetailQuery
    ) -> OrganizationAdministrationView: ...
    def create_organization(
        self, command: CreateOrganizationCommand, *, deployment_id: str
    ) -> OrganizationView: ...
    def rename_organization(
        self, command: RenameOrganizationCommand, *, deployment_id: str
    ) -> OrganizationView: ...
    def suspend_organization(
        self, command: OrganizationMutationCommand, *, deployment_id: str
    ) -> OrganizationView: ...
    def reactivate_organization(
        self, command: OrganizationMutationCommand, *, deployment_id: str
    ) -> OrganizationView: ...
    def revise_organization_policy(
        self, command: ReviseOrganizationPolicyCommand, *, deployment_id: str
    ) -> OrganizationView: ...
    def replace_organization_administrator(
        self, command: ReplaceOrganizationAdministratorCommand, *, deployment_id: str
    ) -> ProvisionedCustomerUserResult: ...
    def issue_administrator_token(
        self,
        command: OrganizationMutationCommand,
        purpose: RecoveryTokenPurpose,
        *,
        deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult: ...
    def list_customer_users(self, query: CustomerUserListQuery) -> CustomerUserPage: ...
    def get_customer_user(self, query: CustomerUserDetailQuery) -> CustomerUserView: ...
    def create_customer_user(
        self, command: CreateCustomerUserCommand, *, deployment_id: str
    ) -> ProvisionedCustomerUserResult: ...
    def activate_customer_user(
        self, command: ActivateCustomerUserCommand, *, deployment_id: str
    ) -> CustomerUserView: ...
    def suspend_customer_user(
        self, command: CustomerUserMutationCommand, *, deployment_id: str
    ) -> CustomerUserView: ...
    def reactivate_customer_user(
        self, command: CustomerUserMutationCommand, *, deployment_id: str
    ) -> CustomerUserView: ...
    def deactivate_customer_user(
        self, command: CustomerUserMutationCommand, *, deployment_id: str
    ) -> CustomerUserView: ...
    def issue_customer_reset(
        self, command: CustomerUserMutationCommand, *, deployment_id: str
    ) -> AuthorizedRecoveryTokenResult: ...
    def reissue_customer_activation(
        self, command: CustomerUserMutationCommand, *, deployment_id: str
    ) -> AuthorizedRecoveryTokenResult: ...
    def reset_customer_password(
        self, command: PasswordResetCommand, *, deployment_id: str
    ) -> CustomerUserView: ...
    def list_security_events(
        self, query: AdministrationEventQuery
    ) -> AdministrationEventPage: ...
    def list_sessions(
        self, query: OrganizationSessionQuery
    ) -> OrganizationSessionPage: ...
    def revoke_session(
        self, command: RevokeOrganizationSessionCommand, *, deployment_id: str
    ) -> None: ...


class ServerIdentifierDigester(Protocol):
    """Protect low-entropy request identifiers with a deployment key."""

    def digest(self, value: str, *, scope: str) -> str: ...


class ServerAccountRecovery(Protocol):
    """Keep public requests opaque and exact issuance separate."""

    def inspect_token_principal(
        self, token: str, purpose: RecoveryTokenPurpose,
    ) -> LoginPrincipalKind | None: ...

    def request_password_reset(
        self, command: PasswordResetRequestCommand, *, deployment_id: str
    ) -> PasswordResetRequestResult: ...
    def issue_authorized(
        self, command: AuthorizedRecoveryTokenCommand, *, deployment_id: str
    ) -> AuthorizedRecoveryTokenResult: ...
    def reset_password(
        self, command: PasswordResetCommand, *, deployment_id: str
    ) -> PasswordResetResult: ...


@dataclass(frozen=True, slots=True)
class ServerAdministrationDependencies:
    """Bind browser services whose repositories own short transactions."""

    authenticator: ServerAuthenticator
    sessions: ServerSessionManager
    organizations: ServerIdentityAdministration
    identifier_digester: ServerIdentifierDigester
    templates: Jinja2Templates
    deployment_id: str
    session_absolute_timeout: timedelta
    account_recovery: ServerAccountRecovery | None = None
    account_admission: ServerAccountAdmission = field(default_factory=ServerAccountAdmission)


def resolve_session(
    dependencies: ServerAdministrationDependencies,
    request: Request,
    *,
    csrf_token: str | None = None,
    unsafe: bool = False,
) -> CurrentSessionView:
    """Resolve a current subject, validating CSRF for every unsafe action."""

    return dependencies.sessions.access(
        AccessServerSessionCommand(
            handle=request.cookies.get(SERVER_SESSION_COOKIE_NAME, ""),
            csrf_token=csrf_token,
            require_csrf=unsafe,
        )
    )


def require_actor(
    session: CurrentSessionView, kind: LoginPrincipalKind
) -> AdministrationActor:
    """Construct authority only from a resolved session of the required role."""

    if session.subject.principal_kind is not kind:
        raise ServerRoleForbiddenError
    return AdministrationActor(
        principal_kind=kind,
        principal_id=session.subject.principal_id,
        organization_id=session.subject.organization_id,
    )


def form_text(form: FormData, name: str) -> str:
    """Read one text field without coercing uploaded files."""

    value = form.get(name)
    return value if isinstance(value, str) else ""


def form_revision(form: FormData, name: str = "expected_revision") -> int:
    """Require one bounded positive optimistic revision from a form."""

    value = form_text(form, name)
    if (
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 18
        or int(value) <= 0
    ):
        raise IdentityAdministrationInputError("Revision is invalid")
    return int(value)


def cursor_time(value: str | None) -> datetime | None:
    """Parse an aware bounded keyset timestamp without accepting naive time."""

    if value is None:
        return None
    try:
        observed = datetime.fromisoformat(value) if len(value) <= 64 else None
        if observed is None or observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError
        return observed
    except ValueError:
        raise IdentityAdministrationInputError("Page cursor is invalid") from None


def render(
    dependencies: ServerAdministrationDependencies,
    request: Request,
    name: str,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> Response:
    """Render one bounded server template with the common browser context."""

    return dependencies.templates.TemplateResponse(
        request=request,
        name=f"server/{name}.html",
        context=context,
        status_code=status_code,
    )


async def run_server_browser_operation(
    dependencies: ServerAdministrationDependencies,
    request: Request,
    operation: Callable[[], Response],
) -> Response:
    """Run complete synchronous service and render work off the event loop."""

    try:
        return await run_in_threadpool(operation)
    except ServerSessionRejectedError:
        response = RedirectResponse("/login", status_code=303)
        clear_server_session_cookie(response)
        return response
    except ServerAccountBusyError:
        message, status = "Account forms are busy. Try again shortly.", 429
    except ServerRoleForbiddenError:
        message, status = "Your account cannot perform this action.", 403
    except (IdentityAdministrationNotFoundError, AccountRecoverySubjectNotEligibleError):
        message, status = "The requested account or Organization is not available.", 404
    except IdentityAdministrationConflictError:
        message, status = (
            "The details changed or conflict with an existing account. Reload the page and review before retrying.",
            409,
        )
    except IdentityAdministrationInputError:
        message, status = "The submitted details or confirmation are invalid.", 400
    except AccountRecoveryRejectedError:
        message, status = (
            "The link could not be used. It may have expired or already been used. Request a new link.",
            400,
        )
    except (
        ServerSessionStorageError,
        ServerSessionUnavailableError,
        IdentityAdministrationStorageError,
        IdentityAdministrationUnavailableError,
        AccountRecoveryStorageError,
        AccountRecoveryUnavailableError,
    ):
        message, status = "Administration is temporarily unavailable.", 503
    return render(
        dependencies,
        request,
        "error",
        {"message": message, "back_url": "/"},
        status_code=status,
    )


def require_confirmation(form: FormData, expected: str) -> None:
    """Require the affected identity and explicit acknowledgement on mutations."""

    if form_text(form, "confirmation") != expected:
        raise IdentityAdministrationInputError("Confirmation is invalid")


def link_result(
    dependencies: ServerAdministrationDependencies,
    request: Request,
    result: AuthorizedRecoveryTokenResult,
    *,
    back_url: str,
) -> Response:
    """Show a raw link once in a non-cacheable response without a query token."""

    route = (
        "activate"
        if result.purpose is RecoveryTokenPurpose.ACTIVATION
        else "reset-password"
    )
    link = f"{str(request.base_url).rstrip('/')}/{route}#{result.token}"
    response = render(
        dependencies,
        request,
        "one_time_link",
        {
            "link": link,
            "expires_at": result.expires_at,
            "purpose": route,
            "back_url": back_url,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
