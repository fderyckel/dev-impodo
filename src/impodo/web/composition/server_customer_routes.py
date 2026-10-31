"""Expose tenant-bound customer lifecycle, session, and security-history pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from impodo.domain.identity.administration import (
    AdministrationEventQuery,
    CreateCustomerUserCommand,
    CustomerUserMutationCommand,
    IdentityAdministrationInputError,
    OrganizationSessionQuery,
    RevokeOrganizationSessionCommand,
)
from impodo.domain.identity.recovery import (
    AuthorizedRecoveryTokenResult,
    LoginPrincipalKind,
    RecoveryTokenPurpose,
)
from impodo.web.composition.server_administration_context import (
    ServerAdministrationDependencies,
    cursor_time,
    form_revision,
    form_text,
    link_result,
    render,
    require_actor,
    require_confirmation,
    resolve_session,
    run_server_browser_operation,
)
from impodo.web.composition.server_session_http import SERVER_CSRF_COOKIE_NAME


def create_server_customer_router(
    dependencies: ServerAdministrationDependencies,
) -> APIRouter:
    """Bind every customer query and mutation to the resolved actor's tenant."""

    router = APIRouter(include_in_schema=False)

    @router.post("/admin/users")
    async def create_user(request: Request) -> Response:
        form = await request.form()

        def create() -> Response:
            session = resolve_session(
                dependencies,
                request,
                csrf_token=form_text(form, "csrf_token"),
                unsafe=True,
            )
            actor = require_actor(
                session, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            )
            user = dependencies.organizations.create_customer_user(
                CreateCustomerUserCommand(
                    actor=actor,
                    organization_id=actor.organization_id or "",
                    login_email=form_text(form, "login_email"),
                    display_name=form_text(form, "display_name"),
                ),
                deployment_id=dependencies.deployment_id,
            )
            return link_result(
                dependencies,
                request,
                AuthorizedRecoveryTokenResult(
                    purpose=RecoveryTokenPurpose.ACTIVATION,
                    token=user.activation_token,
                    expires_at=user.expires_at,
                ),
                back_url=f"/admin/users/{user.user.user_id}",
            )

        return await run_server_browser_operation(dependencies, request, create)

    @router.post("/admin/users/{user_id}/actions")
    async def user_action(request: Request, user_id: str) -> Response:
        form = await request.form()

        def mutate() -> Response:
            session = resolve_session(
                dependencies,
                request,
                csrf_token=form_text(form, "csrf_token"),
                unsafe=True,
            )
            actor = require_actor(
                session, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            )
            require_confirmation(form, user_id)
            command = CustomerUserMutationCommand(
                actor, actor.organization_id or "", user_id, form_revision(form)
            )
            service = dependencies.organizations
            action = form_text(form, "action")
            back_url = f"/admin/users/{user_id}"
            if action in {"reset", "activation"}:
                result = (
                    service.issue_customer_reset(
                        command, deployment_id=dependencies.deployment_id
                    )
                    if action == "reset"
                    else service.reissue_customer_activation(
                        command, deployment_id=dependencies.deployment_id
                    )
                )
                return link_result(dependencies, request, result, back_url=back_url)
            if action == "suspend":
                service.suspend_customer_user(
                    command, deployment_id=dependencies.deployment_id
                )
            elif action == "reactivate":
                service.reactivate_customer_user(
                    command, deployment_id=dependencies.deployment_id
                )
            elif action == "deactivate":
                service.deactivate_customer_user(
                    command, deployment_id=dependencies.deployment_id
                )
            else:
                raise IdentityAdministrationInputError("Customer action is invalid")
            return RedirectResponse(back_url, status_code=303)

        return await run_server_browser_operation(dependencies, request, mutate)

    @router.get("/admin/sessions")
    async def sessions(
        request: Request,
        user_id: str | None = None,
        before_created_at: str | None = None,
        before_session_id: str | None = None,
    ) -> Response:
        def read() -> Response:
            session = resolve_session(dependencies, request)
            actor = require_actor(
                session, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            )
            page = dependencies.organizations.list_sessions(
                OrganizationSessionQuery(
                    actor=actor,
                    organization_id=actor.organization_id or "",
                    user_id=user_id,
                    before_created_at=cursor_time(before_created_at),
                    before_session_id=before_session_id,
                )
            )
            return render(
                dependencies,
                request,
                "sessions",
                {
                    "session": session,
                    "page": page,
                    "user_id": user_id,
                    "csrf_token": request.cookies.get(SERVER_CSRF_COOKIE_NAME, ""),
                },
            )

        return await run_server_browser_operation(dependencies, request, read)

    @router.post("/admin/sessions/{session_id}/revoke")
    async def revoke_session(request: Request, session_id: str) -> Response:
        form = await request.form()

        def revoke() -> Response:
            session = resolve_session(
                dependencies,
                request,
                csrf_token=form_text(form, "csrf_token"),
                unsafe=True,
            )
            actor = require_actor(
                session, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            )
            require_confirmation(form, session_id)
            dependencies.organizations.revoke_session(
                RevokeOrganizationSessionCommand(
                    actor=actor,
                    organization_id=actor.organization_id or "",
                    session_id=session_id,
                    expected_revision=form_revision(form),
                ),
                deployment_id=dependencies.deployment_id,
            )
            return RedirectResponse("/admin/sessions", status_code=303)

        return await run_server_browser_operation(dependencies, request, revoke)

    @router.get("/admin/history")
    async def history(
        request: Request,
        user_id: str | None = None,
        before_occurred_at: str | None = None,
        before_event_id: str | None = None,
    ) -> Response:
        def read() -> Response:
            session = resolve_session(dependencies, request)
            actor = require_actor(
                session, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR
            )
            page = dependencies.organizations.list_security_events(
                AdministrationEventQuery(
                    actor=actor,
                    organization_id=actor.organization_id or "",
                    user_id=user_id,
                    before_occurred_at=cursor_time(before_occurred_at),
                    before_event_id=before_event_id,
                )
            )
            return render(
                dependencies,
                request,
                "history",
                {
                    "session": session,
                    "page": page,
                    "user_id": user_id,
                    "csrf_token": request.cookies.get(SERVER_CSRF_COOKIE_NAME, ""),
                },
            )

        return await run_server_browser_operation(dependencies, request, read)

    return router
