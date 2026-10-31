"""Expose secret-safe activation, recovery, and the signed-in account view."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from impodo.domain.identity.administration import (
    ActivateCustomerUserCommand,
    IdentityAdministrationInputError,
)
from impodo.domain.identity.recovery import (
    PASSWORD_RESET_ACCEPTED_MESSAGE,
    PASSWORD_RESET_REJECTED_MESSAGE,
    AccountRecoveryRejectedError,
    AccountRecoveryUnavailableError,
    AuthorizedRecoveryTokenCommand,
    LoginPrincipalKind,
    PasswordResetCommand,
    PasswordResetRequestCommand,
    RecoveryTokenPurpose,
)
from impodo.web.composition.server_account_admission import ServerAccountBusyError
from impodo.web.composition.server_administration_context import (
    ServerAdministrationDependencies,
    form_text,
    link_result,
    render,
    require_actor,
    require_confirmation,
    resolve_session,
    run_server_browser_operation,
)
from impodo.web.composition.server_session_http import (
    SERVER_CSRF_COOKIE_NAME,
    clear_server_session_cookie,
)

ACCOUNT_FORM_CSRF_COOKIE = "__Host-impodo_account_csrf"


def create_server_account_router(
    dependencies: ServerAdministrationDependencies,
) -> APIRouter:
    """Build public one-time forms whose tokens travel only in fragments and bodies."""

    router = APIRouter(include_in_schema=False)
    admission = dependencies.account_admission

    @router.get("/administration.css")
    async def administration_styles() -> FileResponse:
        return FileResponse(
            Path(__file__).resolve().parents[1]
            / "static"
            / "server-administration.css",
            media_type="text/css",
        )

    @router.get("/account-token.js")
    async def account_token_script() -> FileResponse:
        return FileResponse(
            Path(__file__).resolve().parents[1] / "static" / "server-account-token.js",
            media_type="text/javascript",
        )

    @router.get("/activate")
    async def activate_form(request: Request) -> Response:
        return _account_form(dependencies, request, "activate")

    @router.get("/reset-password")
    async def reset_form(request: Request) -> Response:
        return _account_form(dependencies, request, "reset-password")

    @router.get("/forgot-password")
    async def forgot_form(request: Request) -> Response:
        return _account_form(dependencies, request, "forgot-password")

    @router.post("/activate")
    async def activate(request: Request) -> Response:
        form = await request.form()

        def consume() -> Response:
            _require_public_csrf(request, form_text(form, "csrf_token"))
            with admission.admit(_source_digest(dependencies, request)):
                return activate_admitted()

        def activate_admitted() -> Response:
            _require_password_confirmation(
                form_text(form, "password"), form_text(form, "password_confirmation")
            )
            kind = _token_principal(
                dependencies, form_text(form, "token"), RecoveryTokenPurpose.ACTIVATION,
            )
            if kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
                raise AccountRecoveryRejectedError(PASSWORD_RESET_REJECTED_MESSAGE)
            dependencies.organizations.activate_customer_user(
                ActivateCustomerUserCommand(
                    token=form_text(form, "token"),
                    password=form_text(form, "password"),
                    source_digest=_source_digest(dependencies, request),
                ),
                deployment_id=dependencies.deployment_id,
            )
            response = render(
                dependencies,
                request,
                "account_result",
                {
                    "message": "Your account is active. Sign in with your email and new password.",
                },
            )
            clear_server_session_cookie(response)
            return response

        return await run_server_browser_operation(dependencies, request, consume)

    @router.post("/reset-password")
    async def reset(request: Request) -> Response:
        form = await request.form()

        def consume() -> Response:
            _require_public_csrf(request, form_text(form, "csrf_token"))
            with admission.admit(_source_digest(dependencies, request)):
                return reset_admitted()

        def reset_admitted() -> Response:
            password = form_text(form, "password")
            _require_password_confirmation(
                password, form_text(form, "password_confirmation")
            )
            command = PasswordResetCommand(
                token=form_text(form, "token"),
                new_password=password,
                source_digest=_source_digest(dependencies, request),
            )
            kind = _token_principal(
                dependencies, command.token, RecoveryTokenPurpose.PASSWORD_RESET,
            )
            if kind is LoginPrincipalKind.PLATFORM_ADMINISTRATOR:
                recovery = dependencies.account_recovery
                if recovery is None:
                    raise AccountRecoveryUnavailableError("Account recovery is unavailable")
                recovery.reset_password(command, deployment_id=dependencies.deployment_id)
            else:
                dependencies.organizations.reset_customer_password(
                    command, deployment_id=dependencies.deployment_id
                )
            response = render(
                dependencies,
                request,
                "account_result",
                {
                    "message": "Your password has changed and previous sessions have been revoked. Sign in again.",
                },
            )
            clear_server_session_cookie(response)
            return response

        return await run_server_browser_operation(dependencies, request, consume)

    @router.post("/forgot-password")
    async def forgot(request: Request) -> Response:
        form = await request.form()

        def accept() -> Response:
            _require_public_csrf(request, form_text(form, "csrf_token"))
            if dependencies.account_recovery is not None:
                try:
                    with admission.admit(_source_digest(dependencies, request)):
                        dependencies.account_recovery.request_password_reset(
                            PasswordResetRequestCommand(
                                login_email=form_text(form, "login_email"),
                                source_digest=_source_digest(dependencies, request),
                            ),
                            deployment_id=dependencies.deployment_id,
                        )
                except ServerAccountBusyError:
                    pass  # Public reset requests retain the same opaque acknowledgement.
            return render(
                dependencies,
                request,
                "account_result",
                {
                    "message": PASSWORD_RESET_ACCEPTED_MESSAGE,
                    "guidance": "During this local trial, email delivery is disabled. Ask your Organization Administrator or platform operator for a recovery link.",
                },
            )

        return await run_server_browser_operation(dependencies, request, accept)

    @router.get("/account")
    async def account(request: Request) -> Response:
        def read() -> Response:
            session = resolve_session(dependencies, request)
            return render(
                dependencies,
                request,
                "account",
                {
                    "session": session,
                    "csrf_token": request.cookies.get(SERVER_CSRF_COOKIE_NAME, ""),
                },
            )

        return await run_server_browser_operation(dependencies, request, read)

    @router.post("/account/platform-reset-link")
    async def platform_reset(request: Request) -> Response:
        form = await request.form()

        def issue() -> Response:
            session = resolve_session(
                dependencies,
                request,
                csrf_token=form_text(form, "csrf_token"),
                unsafe=True,
            )
            actor = require_actor(session, LoginPrincipalKind.PLATFORM_ADMINISTRATOR)
            require_confirmation(form, actor.principal_id)
            if dependencies.account_recovery is None:
                raise IdentityAdministrationInputError(
                    "Account recovery is unavailable"
                )
            result = dependencies.account_recovery.issue_authorized(
                AuthorizedRecoveryTokenCommand(
                    principal_kind=actor.principal_kind,
                    principal_id=actor.principal_id,
                    purpose=RecoveryTokenPurpose.PASSWORD_RESET,
                    source_digest=_source_digest(dependencies, request),
                ),
                deployment_id=dependencies.deployment_id,
            )
            return link_result(dependencies, request, result, back_url="/account")

        return await run_server_browser_operation(dependencies, request, issue)

    return router


def _token_principal(
    dependencies: ServerAdministrationDependencies, token: str, purpose: RecoveryTokenPurpose,
) -> LoginPrincipalKind:
    recovery = dependencies.account_recovery
    if recovery is None:
        raise AccountRecoveryUnavailableError("Account recovery is unavailable")
    kind = recovery.inspect_token_principal(token, purpose)
    if kind is None:
        raise AccountRecoveryRejectedError(PASSWORD_RESET_REJECTED_MESSAGE)
    return kind


def _account_form(
    dependencies: ServerAdministrationDependencies, request: Request, mode: str
) -> Response:
    token = secrets.token_urlsafe(32)
    response = render(
        dependencies, request, "account_form", {"mode": mode, "csrf_token": token}
    )
    response.set_cookie(
        ACCOUNT_FORM_CSRF_COOKIE,
        token,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=1800,
    )
    return response


def _require_public_csrf(request: Request, token: str) -> None:
    cookie = request.cookies.get(ACCOUNT_FORM_CSRF_COOKIE, "")
    if (
        not cookie
        or not token
        or len(cookie) > 128
        or len(token) > 128
        or not cookie.isascii()
        or not token.isascii()
        or not hmac.compare_digest(cookie, token)
    ):
        raise IdentityAdministrationInputError("Account form expired")


def _require_password_confirmation(password: str, confirmation: str) -> None:
    if password != confirmation or not 16 <= len(password) <= 1024:
        raise IdentityAdministrationInputError(
            "Password must match and contain 16 to 1024 characters"
        )


def _source_digest(
    dependencies: ServerAdministrationDependencies, request: Request
) -> str:
    return dependencies.identifier_digester.digest(
        request.client.host if request.client else "unknown",
        scope="account-form-source",
    )
