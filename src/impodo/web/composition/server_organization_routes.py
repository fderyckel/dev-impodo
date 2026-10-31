"""Expose Platform Administrator provisioning and Organization confirmations."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from impodo.domain.identity.administration import (
    IdentityAdministrationInputError,
    OrganizationDetailQuery,
    OrganizationMutationCommand,
    RenameOrganizationCommand,
    ReplaceOrganizationAdministratorCommand,
    ReviseOrganizationPolicyCommand,
)
from impodo.domain.identity.recovery import (
    AuthorizedRecoveryTokenResult,
    LoginPrincipalKind,
    RecoveryTokenPurpose,
)
from impodo.web.composition.server_administration_context import (
    ServerAdministrationDependencies,
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


def create_server_organization_router(
    dependencies: ServerAdministrationDependencies,
) -> APIRouter:
    """Build exact Organization reads and revision-bound provisioning actions."""

    router = APIRouter(include_in_schema=False)

    @router.get("/admin/organizations/{organization_id}")
    async def organization_detail(request: Request, organization_id: str) -> Response:
        def read() -> Response:
            session = resolve_session(dependencies, request)
            view = dependencies.organizations.get_organization(
                OrganizationDetailQuery(
                    actor=require_actor(
                        session, LoginPrincipalKind.PLATFORM_ADMINISTRATOR
                    ),
                    organization_id=organization_id,
                )
            )
            return render(
                dependencies,
                request,
                "organization_detail",
                {
                    "view": view,
                    "session": session,
                    "csrf_token": request.cookies.get(SERVER_CSRF_COOKIE_NAME, ""),
                },
            )

        return await run_server_browser_operation(dependencies, request, read)

    @router.post("/admin/organizations/{organization_id}/actions")
    async def organization_action(request: Request, organization_id: str) -> Response:
        form = await request.form()

        def mutate() -> Response:
            session = resolve_session(
                dependencies,
                request,
                csrf_token=form_text(form, "csrf_token"),
                unsafe=True,
            )
            actor = require_actor(session, LoginPrincipalKind.PLATFORM_ADMINISTRATOR)
            require_confirmation(form, organization_id)
            mutation = OrganizationMutationCommand(
                actor, organization_id, form_revision(form)
            )
            service = dependencies.organizations
            action = form_text(form, "action")
            back_url = f"/admin/organizations/{organization_id}"
            if action == "assign-administrator":
                provisioned = service.replace_organization_administrator(
                    ReplaceOrganizationAdministratorCommand(
                        actor=actor,
                        organization_id=organization_id,
                        login_email=form_text(form, "login_email"),
                        display_name=form_text(form, "display_name"),
                        expected_organization_revision=mutation.expected_revision,
                    ),
                    deployment_id=dependencies.deployment_id,
                )
                return link_result(
                    dependencies,
                    request,
                    AuthorizedRecoveryTokenResult(
                        purpose=RecoveryTokenPurpose.ACTIVATION,
                        token=provisioned.activation_token,
                        expires_at=provisioned.expires_at,
                    ),
                    back_url=back_url,
                )
            if action in {"administrator-activation", "administrator-reset"}:
                purpose = (
                    RecoveryTokenPurpose.ACTIVATION
                    if action == "administrator-activation"
                    else RecoveryTokenPurpose.PASSWORD_RESET
                )
                result = service.issue_administrator_token(
                    mutation, purpose, deployment_id=dependencies.deployment_id
                )
                return link_result(dependencies, request, result, back_url=back_url)
            if action == "rename":
                service.rename_organization(
                    RenameOrganizationCommand(
                        mutation, form_text(form, "display_name")
                    ),
                    deployment_id=dependencies.deployment_id,
                )
            elif action == "suspend":
                service.suspend_organization(
                    mutation, deployment_id=dependencies.deployment_id
                )
            elif action == "reactivate":
                service.reactivate_organization(
                    mutation, deployment_id=dependencies.deployment_id
                )
            elif action == "reauthenticate":
                service.revise_organization_policy(
                    ReviseOrganizationPolicyCommand(
                        mutation,
                        form_revision(form, "expected_policy_revision"),
                    ),
                    deployment_id=dependencies.deployment_id,
                )
            else:
                raise IdentityAdministrationInputError("Organization action is invalid")
            return RedirectResponse(back_url, status_code=303)

        return await run_server_browser_operation(dependencies, request, mutate)

    return router
