"""Expose the first authenticated shared-server administration journey."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from impodo.domain.identity.administration import (
    AdministrationActor,
    CreateOrganizationCommand,
    IdentityAdministrationConflictError,
    IdentityAdministrationInputError,
    IdentityAdministrationNotFoundError,
    IdentityAdministrationStorageError,
    IdentityAdministrationUnavailableError,
    OrganizationListQuery,
    OrganizationPage,
    OrganizationView,
)
from impodo.domain.identity.authentication import (
    PlatformAuthenticationCommand,
    PlatformAuthenticationRejectedError,
    PlatformAuthenticationResult,
    PlatformAuthenticationStorageError,
    PlatformAuthenticationUnavailableError,
)
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    SESSION_REJECTED_MESSAGE,
    AccessServerSessionCommand,
    CreateServerSessionCommand,
    CurrentSessionView,
    RevokeServerSessionCommand,
    ServerSessionConflictError,
    ServerSessionCredentials,
    ServerSessionRejectedError,
    ServerSessionStorageError,
    ServerSessionUnavailableError,
    SessionAuthenticationMethod,
    SessionSubject,
)
from impodo.web.composition.server_session_http import (
    SERVER_CSRF_COOKIE_NAME,
    SERVER_SESSION_COOKIE_NAME,
    clear_server_session_cookie,
    set_server_session_cookie,
)

_AUTHENTICATION_ERROR = "Login was not accepted."
_UNAVAILABLE_ERROR = "Administration is temporarily unavailable."


class ServerPlatformAuthenticator(Protocol):
    """Authenticate one Platform Administrator."""

    def authenticate(
        self,
        command: PlatformAuthenticationCommand,
        *,
        deployment_id: str,
    ) -> PlatformAuthenticationResult: ...


class ServerSessionManager(Protocol):
    """Create, resolve, and revoke Platform Administrator sessions."""

    def create(
        self,
        command: CreateServerSessionCommand,
        *,
        deployment_id: str,
    ) -> ServerSessionCredentials: ...

    def access(self, command: AccessServerSessionCommand) -> CurrentSessionView: ...

    def revoke(
        self,
        command: RevokeServerSessionCommand,
        *,
        deployment_id: str,
    ) -> None: ...


class ServerOrganizationAdministration(Protocol):
    """Read and create Organizations for one Platform Administrator."""

    def list_organizations(self, query: OrganizationListQuery) -> OrganizationPage: ...

    def create_organization(
        self,
        command: CreateOrganizationCommand,
        *,
        deployment_id: str,
    ) -> OrganizationView: ...


class ServerIdentifierDigester(Protocol):
    """Protect a low-entropy request identifier with a deployment key."""

    def digest(self, value: str, *, scope: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ServerAdministrationDependencies:
    """Bind authenticated dashboard services without process-wide connections."""

    authenticator: ServerPlatformAuthenticator
    sessions: ServerSessionManager
    organizations: ServerOrganizationAdministration
    identifier_digester: ServerIdentifierDigester
    templates: Jinja2Templates
    deployment_id: str
    session_absolute_timeout: timedelta


def create_server_administration_router(
    dependencies: ServerAdministrationDependencies,
) -> APIRouter:
    """Build server-only login and Platform Organization routes."""

    router = APIRouter(include_in_schema=False)

    @router.get("/")
    async def server_root() -> RedirectResponse:
        return RedirectResponse("/admin/organizations", status_code=303)

    @router.get("/login")
    async def login_form(request: Request) -> Response:
        return _template(
            dependencies,
            request,
            "server/login.html",
            {"error": None},
        )

    @router.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        login_email = _form_text(form, "login_email")
        password = _form_text(form, "password")
        source = request.client.host if request.client is not None else "unknown"
        source_digest = dependencies.identifier_digester.digest(
            source,
            scope="authentication-source",
        )
        try:
            credentials = await run_in_threadpool(
                _authenticate_and_create_session,
                dependencies,
                login_email,
                password,
                source_digest,
            )
        except PlatformAuthenticationRejectedError:
            return _template(
                dependencies,
                request,
                "server/login.html",
                {"error": _AUTHENTICATION_ERROR},
                status_code=401,
            )
        except (
            PlatformAuthenticationStorageError,
            PlatformAuthenticationUnavailableError,
            ServerSessionStorageError,
            ServerSessionUnavailableError,
        ):
            return _template(
                dependencies,
                request,
                "server/login.html",
                {"error": _UNAVAILABLE_ERROR},
                status_code=503,
            )
        response = RedirectResponse("/admin/organizations", status_code=303)
        set_server_session_cookie(
            response,
            credentials,
            absolute_timeout=dependencies.session_absolute_timeout,
        )
        return response

    @router.get("/admin/organizations")
    async def organizations(
        request: Request,
        after_display_name: str | None = None,
        after_organization_id: str | None = None,
    ) -> Response:
        handle = request.cookies.get(SERVER_SESSION_COOKIE_NAME, "")
        csrf_token = request.cookies.get(SERVER_CSRF_COOKIE_NAME, "")
        try:
            session, page = await run_in_threadpool(
                _load_organizations,
                dependencies,
                handle,
                after_display_name,
                after_organization_id,
            )
        except (
            ServerSessionRejectedError,
            IdentityAdministrationNotFoundError,
        ):
            return _login_redirect()
        except IdentityAdministrationInputError:
            return _template(
                dependencies,
                request,
                "server/error.html",
                {"message": "The Organization page cursor is invalid."},
                status_code=400,
            )
        except (
            ServerSessionStorageError,
            ServerSessionUnavailableError,
            IdentityAdministrationStorageError,
            IdentityAdministrationUnavailableError,
        ):
            return _template(
                dependencies,
                request,
                "server/error.html",
                {"message": _UNAVAILABLE_ERROR},
                status_code=503,
            )
        if not csrf_token:
            return _login_redirect()
        return _template(
            dependencies,
            request,
            "server/organizations.html",
            {
                "csrf_token": csrf_token,
                "page": page,
                "session": session,
            },
        )

    @router.post("/admin/organizations")
    async def create_organization(request: Request) -> Response:
        form = await request.form()
        handle = request.cookies.get(SERVER_SESSION_COOKIE_NAME, "")
        csrf_token = _form_text(form, "csrf_token")
        organization_id = _form_text(form, "organization_id")
        display_name = _form_text(form, "display_name")
        try:
            await run_in_threadpool(
                _create_organization,
                dependencies,
                handle,
                csrf_token,
                organization_id,
                display_name,
            )
        except (
            ServerSessionRejectedError,
            IdentityAdministrationNotFoundError,
        ):
            return _login_redirect()
        except IdentityAdministrationInputError:
            return _template(
                dependencies,
                request,
                "server/error.html",
                {"message": "Organization details are invalid."},
                status_code=400,
            )
        except IdentityAdministrationConflictError:
            return _template(
                dependencies,
                request,
                "server/error.html",
                {"message": "That Organization already exists."},
                status_code=409,
            )
        except (
            ServerSessionStorageError,
            ServerSessionUnavailableError,
            IdentityAdministrationStorageError,
            IdentityAdministrationUnavailableError,
        ):
            return _template(
                dependencies,
                request,
                "server/error.html",
                {"message": _UNAVAILABLE_ERROR},
                status_code=503,
            )
        return RedirectResponse("/admin/organizations", status_code=303)

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        form = await request.form()
        handle = request.cookies.get(SERVER_SESSION_COOKIE_NAME, "")
        csrf_token = _form_text(form, "csrf_token")
        try:
            await run_in_threadpool(
                _revoke_current_session,
                dependencies,
                handle,
                csrf_token,
            )
        except (
            ServerSessionConflictError,
            ServerSessionRejectedError,
            ServerSessionStorageError,
            ServerSessionUnavailableError,
        ):
            pass
        response = RedirectResponse("/login", status_code=303)
        clear_server_session_cookie(response)
        return response

    return router


def _authenticate_and_create_session(
    dependencies: ServerAdministrationDependencies,
    login_email: str,
    password: str,
    source_digest: str,
) -> ServerSessionCredentials:
    authenticated = dependencies.authenticator.authenticate(
        PlatformAuthenticationCommand(
            login_email=login_email,
            password=password,
            source_digest=source_digest,
        ),
        deployment_id=dependencies.deployment_id,
    )
    return dependencies.sessions.create(
        CreateServerSessionCommand(
            subject=SessionSubject(
                principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
                principal_id=authenticated.administrator_id,
                organization_id=None,
            ),
            authentication_method=SessionAuthenticationMethod.PASSWORD,
            security_revision=authenticated.security_revision,
        ),
        deployment_id=dependencies.deployment_id,
    )


def _load_organizations(
    dependencies: ServerAdministrationDependencies,
    handle: str,
    after_display_name: str | None,
    after_organization_id: str | None,
) -> tuple[CurrentSessionView, OrganizationPage]:
    session = dependencies.sessions.access(AccessServerSessionCommand(handle=handle))
    actor = _platform_actor(session)
    page = dependencies.organizations.list_organizations(
        OrganizationListQuery(
            actor=actor,
            after_display_name=after_display_name,
            after_organization_id=after_organization_id,
        )
    )
    return session, page


def _create_organization(
    dependencies: ServerAdministrationDependencies,
    handle: str,
    csrf_token: str,
    organization_id: str,
    display_name: str,
) -> OrganizationView:
    session = dependencies.sessions.access(
        AccessServerSessionCommand(
            handle=handle,
            csrf_token=csrf_token,
            require_csrf=True,
        )
    )
    return dependencies.organizations.create_organization(
        CreateOrganizationCommand(
            actor=_platform_actor(session),
            organization_id=organization_id,
            display_name=display_name,
        ),
        deployment_id=dependencies.deployment_id,
    )


def _revoke_current_session(
    dependencies: ServerAdministrationDependencies,
    handle: str,
    csrf_token: str,
) -> None:
    session = dependencies.sessions.access(
        AccessServerSessionCommand(
            handle=handle,
            csrf_token=csrf_token,
            require_csrf=True,
        )
    )
    actor = _platform_actor(session)
    dependencies.sessions.revoke(
        RevokeServerSessionCommand(
            actor_administrator_id=actor.principal_id,
            session_id=session.session_id,
            expected_revision=session.optimistic_revision,
        ),
        deployment_id=dependencies.deployment_id,
    )


def _platform_actor(session: CurrentSessionView) -> AdministrationActor:
    if session.subject.principal_kind is not LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
        raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE)
    return AdministrationActor(
        principal_kind=session.subject.principal_kind,
        principal_id=session.subject.principal_id,
        organization_id=None,
    )


def _login_redirect() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    clear_server_session_cookie(response)
    return response


def _form_text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _template(
    dependencies: ServerAdministrationDependencies,
    request: Request,
    name: str,
    context: dict[str, object],
    *,
    status_code: int = 200,
) -> Response:
    return dependencies.templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


__all__ = [
    "ServerAdministrationDependencies",
    "create_server_administration_router",
]
