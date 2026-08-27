"""Loopback-browser host, origin, session, CSRF, and response controls.

The middleware rejects proxy/forwarding ambiguity and cross-origin unsafe
requests before a route runs. Route helpers then require the launch-token
session and constant-time CSRF match. The workspace access middleware resolves
the real parent Project before a workspace route can open child state, while
application services still enforce the exact command capability.
"""

from __future__ import annotations

from collections.abc import Callable
from hmac import compare_digest
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from impodo.domain.shared.access import Actor, AuthorizationError, Capability
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
    bind_workspace_access_context,
)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_REQUEST_BYTES = 101 * 1024 * 1024
FORWARDED_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
    }
)


class LoopbackSecurityMiddleware(BaseHTTPMiddleware):
    """Reject untrusted browser origins and add restrictive response headers."""

    def __init__(self, app, *, expected_host: str) -> None:
        super().__init__(app)
        self.expected_host = expected_host
        self.expected_origin = f"http://{expected_host}"

    async def dispatch(self, request: Request, call_next) -> Response:
        """Reject untrusted request metadata, then secure every response."""

        if request.headers.get("host") != self.expected_host:
            return self._secure(PlainTextResponse("Invalid host", status_code=400))
        if any(header in request.headers for header in FORWARDED_HEADERS):
            return self._secure(
                PlainTextResponse("Forwarded requests are not accepted", status_code=400)
            )
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            fetch_site = request.headers.get("sec-fetch-site")
            source_origin = origin or _referer_origin(referer)
            if (
                source_origin != self.expected_origin
                or fetch_site not in {None, "same-origin"}
            ):
                return self._secure(
                    PlainTextResponse("Untrusted request origin", status_code=403)
                )
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_REQUEST_BYTES:
                        return self._secure(
                            PlainTextResponse(
                                "Request is too large",
                                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            )
                        )
                except ValueError:
                    return self._secure(
                        PlainTextResponse("Invalid content length", status_code=400)
                    )

        response = await call_next(request)
        return self._secure(response)

    def _secure(self, response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'none'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "script-src 'self'; "
            "style-src 'self'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Vary"] = "Origin, Sec-Fetch-Site"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


class WorkspaceAccessMiddleware(BaseHTTPMiddleware):
    """Authorize a workspace route before its child stores can open."""

    def __init__(
        self,
        app,
        *,
        access: WorkspaceAccessService,
        actor: Actor | Callable[[], Actor],
        trusted_context_resolver: (
            Callable[[str, str], WorkspaceAccessContext | None] | None
        ) = None,
        route_policy: (
            Callable[[Request, WorkspaceAccessContext], Response | None] | None
        ) = None,
    ) -> None:
        super().__init__(app)
        self.access = access
        self.actor = actor if callable(actor) else lambda: actor
        self.trusted_context_resolver = trusted_context_resolver
        self.route_policy = route_policy

    async def dispatch(self, request: Request, call_next) -> Response:
        """Resolve one safe Project-owned context or return an opaque 404."""

        workspace_id = _workspace_id_from_path(request.url.path)
        if (
            workspace_id is None
            or request.session.get("authenticated") is not True
        ):
            return await call_next(request)
        try:
            trusted_context = (
                self.trusted_context_resolver(request.url.path, workspace_id)
                if self.trusted_context_resolver is not None
                else None
            )
            if trusted_context is None:
                context = self.access.resolve(
                    workspace_id,
                    actor=self.actor(),
                    capability=Capability.PROJECT_VIEW,
                )
            else:
                with bind_workspace_access_context(trusted_context):
                    context = self.access.resolve(
                        workspace_id,
                        actor=self.actor(),
                        capability=Capability.PROJECT_VIEW,
                    )
        except (AuthorizationError, MigrationFoundationError):
            return PlainTextResponse("Workspace not found", status_code=404)
        request.state.workspace_access_context = context
        with bind_workspace_access_context(context):
            try:
                if self.route_policy is not None:
                    policy_response = self.route_policy(request, context)
                    if policy_response is not None:
                        return policy_response
            except (AuthorizationError, MigrationFoundationError):
                return PlainTextResponse("Workspace not found", status_code=404)
            response = await call_next(request)
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            return response

        async def authorized_body():
            with bind_workspace_access_context(context):
                async for chunk in body_iterator:
                    yield chunk

        response.body_iterator = authorized_body()
        return response


def _referer_origin(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _workspace_id_from_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) < 3 or parts[1] != "workspaces" or not parts[2]:
        return None
    return parts[2]


def require_session(request: Request) -> None:
    """Require the browser session established by the local launch token."""

    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="Launch Impodo again")


def require_csrf(request: Request, submitted_token: str) -> None:
    """Require an authenticated session and constant-time CSRF token match."""

    require_session(request)
    expected = request.session.get("csrf_token", "")
    if not expected or not compare_digest(str(expected), submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
