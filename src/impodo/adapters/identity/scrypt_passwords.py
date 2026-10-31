"""Derive versioned memory-hard password hashes with scrypt."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable

from impodo.domain.identity.bootstrap import PasswordHash

SCRYPT_COST = 32_768
SCRYPT_BLOCK_SIZE = 8
SCRYPT_PARALLELIZATION = 1
SCRYPT_DIGEST_BYTES = 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAX_MEMORY_BYTES = 64 * 1024 * 1024


class ScryptPasswordHasher:
    """Create, verify, and identify stale bounded scrypt password hashes."""

    def __init__(
        self,
        *,
        salt_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._salt_factory = salt_factory

    def hash(self, password: str) -> PasswordHash:
        """Derive one hash without returning or logging the raw password."""

        salt = self._salt_factory(SCRYPT_SALT_BYTES)
        if len(salt) != SCRYPT_SALT_BYTES:
            raise ValueError("scrypt salt factory returned an invalid salt")
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_COST,
            r=SCRYPT_BLOCK_SIZE,
            p=SCRYPT_PARALLELIZATION,
            maxmem=SCRYPT_MAX_MEMORY_BYTES,
            dklen=SCRYPT_DIGEST_BYTES,
        )
        return PasswordHash(
            algorithm="scrypt",
            parameters=(
                ("n", SCRYPT_COST),
                ("r", SCRYPT_BLOCK_SIZE),
                ("p", SCRYPT_PARALLELIZATION),
                ("dklen", SCRYPT_DIGEST_BYTES),
            ),
            salt=salt,
            digest=digest,
        )

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        """Compare one candidate against only allowlisted scrypt parameters."""

        parameters = _safe_scrypt_parameters(password_hash)
        if parameters is None:
            return False
        candidate = password
        if not 1 <= len(password) <= 1024 or "\x00" in password:
            candidate = "invalid-password-candidate"
        digest = hashlib.scrypt(
            candidate.encode("utf-8"),
            salt=password_hash.salt,
            n=parameters[0],
            r=parameters[1],
            p=parameters[2],
            maxmem=SCRYPT_MAX_MEMORY_BYTES,
            dklen=parameters[3],
        )
        return hmac.compare_digest(digest, password_hash.digest)

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        """Return whether a verified hash differs from the current policy."""

        return (
            password_hash.algorithm != "scrypt"
            or dict(password_hash.parameters)
            != {
                "n": SCRYPT_COST,
                "r": SCRYPT_BLOCK_SIZE,
                "p": SCRYPT_PARALLELIZATION,
                "dklen": SCRYPT_DIGEST_BYTES,
            }
            or len(password_hash.salt) != SCRYPT_SALT_BYTES
            or len(password_hash.digest) != SCRYPT_DIGEST_BYTES
        )


def _safe_scrypt_parameters(
    password_hash: PasswordHash,
) -> tuple[int, int, int, int] | None:
    if password_hash.algorithm != "scrypt":
        return None
    parameters = dict(password_hash.parameters)
    if set(parameters) != {"n", "r", "p", "dklen"}:
        return None
    n = parameters["n"]
    r = parameters["r"]
    p = parameters["p"]
    digest_length = parameters["dklen"]
    if (
        n < 16_384
        or n > 65_536
        or n & (n - 1) != 0
        or not 1 <= r <= 16
        or not 1 <= p <= 4
        or not 32 <= digest_length <= 64
        or not 16 <= len(password_hash.salt) <= 128
        or len(password_hash.digest) != digest_length
    ):
        return None
    return n, r, p, digest_length


__all__ = [
    "SCRYPT_BLOCK_SIZE",
    "SCRYPT_COST",
    "SCRYPT_DIGEST_BYTES",
    "SCRYPT_MAX_MEMORY_BYTES",
    "SCRYPT_PARALLELIZATION",
    "SCRYPT_SALT_BYTES",
    "ScryptPasswordHasher",
]
