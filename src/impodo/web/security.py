"""Loopback-browser host, origin, session, CSRF, and response controls.

The middleware rejects proxy/forwarding ambiguity and cross-origin unsafe
requests before a route runs. Route helpers then require the launch-token
session and constant-time CSRF match. Capability authorization remains a
separate application-service responsibility in ``access.py``.
"""

from __future__ import annotations

from hmac import compare_digest
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from ..build_contract import (
    ApplicationBuildContract,
    calculate_application_build_contract,
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


class BuildConsistencyMiddleware(BaseHTTPMiddleware):
    """Stop requests when an editable installation changes under the server."""

    def __init__(
        self,
        app,
        *,
        expected: ApplicationBuildContract,
        check_interval_seconds: float = 1.0,
    ) -> None:
        super().__init__(app)
        self.expected = expected
        self.check_interval_seconds = max(0.0, float(check_interval_seconds))
        self._lock = RLock()
        self._next_check = 0.0
        self._changed = False

    async def dispatch(self, request: Request, call_next) -> Response:
        """Check one bounded process-wide fingerprint before route dispatch."""

        if self._application_changed():
            return PlainTextResponse(
                "Impodo was updated while it was open. Restart Impodo before "
                "continuing. Your saved work is unchanged.",
                status_code=409,
            )
        return await call_next(request)

    def _application_changed(self) -> bool:
        with self._lock:
            if self._changed:
                return True
            now = monotonic()
            if now < self._next_check:
                return False
            self._next_check = now + self.check_interval_seconds
            self._changed = calculate_application_build_contract() != self.expected
            return self._changed


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
