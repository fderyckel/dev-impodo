"""Enforce browser transport policy for authenticated server sessions."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from impodo.domain.identity.sessions import ServerSessionCredentials

SERVER_SESSION_COOKIE_NAME = "__Host-impodo_session"
SERVER_CSRF_COOKIE_NAME = "__Host-impodo_csrf"
_HEALTH_PATHS = frozenset({"/health/live", "/health/ready"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    # HTML navigation POSTs use an opaque Origin under no-referrer. Keep the
    # same-origin value so exact Origin validation also works in real browsers.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ServerBrowserSecurityMiddleware:
    """Validate the external origin and attach fail-closed browser headers."""

    def __init__(self, app: ASGIApp, *, public_url: str) -> None:
        self._app = app
        self._public_url = public_url
        split_url = urlsplit(public_url)
        if split_url.scheme != "https" or not split_url.netloc:
            raise ValueError("public_url must be an HTTPS origin")
        self._public_host = split_url.netloc

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path not in _HEALTH_PATHS:
            headers = Headers(scope=scope)
            if headers.get("host") != self._public_host:
                await self._reject(scope, receive, send, status_code=400)
                return
            if (
                str(scope.get("method", "GET")).upper() in _UNSAFE_METHODS
                and headers.get("origin") != self._public_url
            ):
                await self._reject(scope, receive, send, status_code=403)
                return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    if name == "Referrer-Policy" and response_headers.get(name) == "no-referrer":
                        continue
                    response_headers[name] = value
            await send(message)

        await self._app(scope, receive, send_with_security_headers)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
    ) -> None:
        response = JSONResponse(
            {"detail": "Request rejected"},
            status_code=status_code,
            headers=_SECURITY_HEADERS,
        )
        await response(scope, receive, send)


def set_server_session_cookie(
    response: Response,
    credentials: ServerSessionCredentials,
    *,
    absolute_timeout: timedelta,
) -> None:
    """Set the opaque handle with the mandatory __Host cookie attributes."""

    max_age = int(absolute_timeout.total_seconds())
    if max_age <= 0:
        raise ValueError("absolute_timeout must be positive")
    response.set_cookie(
        SERVER_SESSION_COOKIE_NAME,
        credentials.handle,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        SERVER_CSRF_COOKIE_NAME,
        credentials.csrf_token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )


def clear_server_session_cookie(response: Response) -> None:
    """Expire the opaque handle using the same mandatory cookie scope."""

    response.delete_cookie(
        SERVER_SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        SERVER_CSRF_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )


__all__ = [
    "SERVER_CSRF_COOKIE_NAME",
    "SERVER_SESSION_COOKIE_NAME",
    "ServerBrowserSecurityMiddleware",
    "clear_server_session_cookie",
    "set_server_session_cookie",
]
