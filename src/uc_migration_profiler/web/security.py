"""Loopback-browser security controls."""

from __future__ import annotations

from hmac import compare_digest

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response


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
        if request.headers.get("host") != self.expected_host:
            return self._secure(PlainTextResponse("Invalid host", status_code=400))
        if any(header in request.headers for header in FORWARDED_HEADERS):
            return self._secure(
                PlainTextResponse("Forwarded requests are not accepted", status_code=400)
            )
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if origin != self.expected_origin or fetch_site != "same-origin":
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
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def require_session(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="Launch Impodo again")


def require_csrf(request: Request, submitted_token: str) -> None:
    require_session(request)
    expected = request.session.get("csrf_token", "")
    if not expected or not compare_digest(str(expected), submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
