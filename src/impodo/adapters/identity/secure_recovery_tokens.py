"""Generate and verify opaque selector-plus-secret recovery tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from uuid import UUID, uuid4

from impodo.domain.identity.recovery import (
    AccountRecoveryInputError,
    RecoveryTokenMaterial,
    RecoveryTokenProof,
    RecoveryTokenPurpose,
)

_TOKEN_DOMAIN = b"impodo-recovery-token-v1\x00"
_TOKEN_SECRET_BYTES = 32


class SecureRecoveryTokenFactory:
    """Keep raw high-entropy recovery secrets outside persistence."""

    def create(self, purpose: RecoveryTokenPurpose) -> RecoveryTokenMaterial:
        """Create one URL-safe token and its purpose-bound stored digest."""

        token_id = str(uuid4())
        secret = secrets.token_bytes(_TOKEN_SECRET_BYTES)
        encoded_secret = base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
        token = f"{token_id}.{encoded_secret}"
        return RecoveryTokenMaterial(
            token_id=token_id,
            token=token,
            digest=_digest_secret(purpose, secret),
        )

    def prove(
        self,
        token: str,
        purpose: RecoveryTokenPurpose,
    ) -> RecoveryTokenProof:
        """Parse a presented token and derive its purpose-bound digest."""

        try:
            token_id_value, encoded_secret = token.split(".", maxsplit=1)
            token_id = str(UUID(token_id_value))
            padding = "=" * (-len(encoded_secret) % 4)
            secret = base64.b64decode(
                encoded_secret + padding,
                altchars=b"-_",
                validate=True,
            )
            if len(secret) != _TOKEN_SECRET_BYTES:
                raise ValueError
        except (ValueError, UnicodeEncodeError) as error:
            raise AccountRecoveryInputError("Recovery token is invalid") from error
        return RecoveryTokenProof(
            token_id=token_id,
            digest=_digest_secret(purpose, secret),
        )


def _digest_secret(purpose: RecoveryTokenPurpose, secret: bytes) -> bytes:
    return hashlib.sha256(
        _TOKEN_DOMAIN + purpose.value.encode("ascii") + b"\x00" + secret
    ).digest()


class HmacIdentifierDigester:
    """Protect low-entropy account and source values with a deployment key."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("Identifier digest key must contain at least 32 bytes")
        self._key = key

    def digest(self, value: str, *, scope: str) -> str:
        """Return a schema-compatible, scope-separated HMAC digest."""

        if not scope or "\x00" in scope:
            raise ValueError("Identifier digest scope is invalid")
        digest = hmac.new(
            self._key,
            scope.encode("utf-8") + b"\x00" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256:{digest}"


__all__ = ["HmacIdentifierDigester", "SecureRecoveryTokenFactory"]
