"""Generate opaque server-session handles and CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import secrets
from uuid import UUID, uuid4

from impodo.domain.identity.sessions import (
    ServerSessionInputError,
    SessionProof,
    SessionSecretMaterial,
)

_HANDLE_DOMAIN = b"impodo-server-session-handle-v1\x00"
_CSRF_DOMAIN = b"impodo-server-session-csrf-v1\x00"
_SECRET_BYTES = 32


class SecureSessionSecretFactory:
    """Keep high-entropy browser secrets outside PostgreSQL."""

    def create(self) -> SessionSecretMaterial:
        """Create a selector-plus-secret handle and independent CSRF token."""

        return self.rotate(str(uuid4()))

    def rotate(self, session_id: str) -> SessionSecretMaterial:
        """Create replacement secrets for an existing session selector."""

        try:
            canonical_session_id = str(UUID(session_id))
        except (TypeError, ValueError) as error:
            raise ServerSessionInputError("Session identifier is invalid") from error
        handle_secret = secrets.token_bytes(_SECRET_BYTES)
        csrf_secret = secrets.token_bytes(_SECRET_BYTES)
        encoded_handle = _encode(handle_secret)
        csrf_token = _encode(csrf_secret)
        return SessionSecretMaterial(
            session_id=canonical_session_id,
            handle=f"{canonical_session_id}.{encoded_handle}",
            handle_digest=_digest(_HANDLE_DOMAIN, handle_secret),
            csrf_token=csrf_token,
            csrf_digest=_digest(_CSRF_DOMAIN, csrf_secret),
        )

    def prove(self, handle: str, csrf_token: str | None = None) -> SessionProof:
        """Derive stored digests from presented browser credentials."""

        try:
            session_id_value, encoded_secret = handle.split(".", maxsplit=1)
            session_id = str(UUID(session_id_value))
            handle_secret = _decode(encoded_secret)
            csrf_secret = _decode(csrf_token) if csrf_token is not None else None
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ServerSessionInputError("Session credentials are invalid") from error
        return SessionProof(
            session_id=session_id,
            handle_digest=_digest(_HANDLE_DOMAIN, handle_secret),
            csrf_digest=(
                _digest(_CSRF_DOMAIN, csrf_secret)
                if csrf_secret is not None
                else None
            ),
        )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str | None) -> bytes:
    if value is None:
        raise TypeError
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if len(decoded) != _SECRET_BYTES:
        raise ValueError
    return decoded


def _digest(domain: bytes, value: bytes) -> bytes:
    return hashlib.sha256(domain + value).digest()


__all__ = ["SecureSessionSecretFactory"]
