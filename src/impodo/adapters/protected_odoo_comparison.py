"""Application-level encryption for protected Odoo comparison artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..domain.odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ..workspace_errors import WorkspaceError


_MAGIC = b"IPODOCMP1"
_NONCE_BYTES = 12


@dataclass(frozen=True, slots=True)
class EncodedOdooComparison:
    """Encrypted bytes plus hashes for one protected comparison payload."""

    encrypted_bytes: bytes
    logical_hash: str
    artifact_hash: str


def encode_odoo_comparison(
    plaintext: bytes,
    *,
    authenticated_binding: bytes,
    key: bytes,
) -> EncodedOdooComparison:
    """Encrypt bounded comparison JSON with its immutable run binding as AAD."""

    _require_key(key)
    if not plaintext or len(plaintext) > CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes:
        raise WorkspaceError("Protected Odoo comparison size is invalid")
    nonce = os.urandom(_NONCE_BYTES)
    encrypted = _MAGIC + nonce + AESGCM(key).encrypt(
        nonce,
        plaintext,
        authenticated_binding,
    )
    return EncodedOdooComparison(
        encrypted_bytes=encrypted,
        logical_hash=_hash(plaintext),
        artifact_hash=_hash(encrypted),
    )


def decode_odoo_comparison(
    encrypted_bytes: bytes,
    *,
    authenticated_binding: bytes,
    expected_logical_hash: str,
    expected_artifact_hash: str,
    key: bytes,
) -> bytes:
    """Authenticate, decrypt, and verify one bounded comparison artifact."""

    _require_key(key)
    maximum = CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes + len(_MAGIC) + 64
    if len(encrypted_bytes) > maximum or _hash(encrypted_bytes) != expected_artifact_hash:
        raise WorkspaceError("Protected Odoo comparison failed verification")
    if not encrypted_bytes.startswith(_MAGIC):
        raise WorkspaceError("Protected Odoo comparison header is invalid")
    start = len(_MAGIC)
    nonce = encrypted_bytes[start : start + _NONCE_BYTES]
    ciphertext = encrypted_bytes[start + _NONCE_BYTES :]
    if len(nonce) != _NONCE_BYTES or len(ciphertext) <= 16:
        raise WorkspaceError("Protected Odoo comparison is incomplete")
    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            authenticated_binding,
        )
    except InvalidTag as error:
        raise WorkspaceError(
            "Protected Odoo comparison authentication failed"
        ) from error
    if _hash(plaintext) != expected_logical_hash:
        raise WorkspaceError("Protected Odoo comparison content changed")
    return plaintext


def _hash(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _require_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise WorkspaceError("Protected Odoo comparison key is invalid")
