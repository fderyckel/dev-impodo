"""Verify the initial password hash is slow, salted, and self-describing."""

from __future__ import annotations

import unittest

from impodo.adapters.identity.scrypt_passwords import (
    SCRYPT_COST,
    SCRYPT_DIGEST_BYTES,
    SCRYPT_SALT_BYTES,
    ScryptPasswordHasher,
)


class ScryptPasswordHasherTests(unittest.TestCase):
    def test_hash_contains_policy_metadata_without_the_password(self) -> None:
        salt = b"s" * SCRYPT_SALT_BYTES
        hashed = ScryptPasswordHasher(salt_factory=lambda _size: salt).hash(
            "fictional-bootstrap-password"
        )

        self.assertEqual(hashed.algorithm, "scrypt")
        self.assertEqual(dict(hashed.parameters)["n"], SCRYPT_COST)
        self.assertEqual(hashed.salt, salt)
        self.assertEqual(len(hashed.digest), SCRYPT_DIGEST_BYTES)
        self.assertNotIn(b"fictional-bootstrap-password", hashed.digest)

    def test_invalid_salt_factory_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid salt"):
            ScryptPasswordHasher(salt_factory=lambda _size: b"short").hash(
                "fictional-bootstrap-password"
            )

    def test_verify_uses_constant_time_digest_comparison_and_current_policy(
        self,
    ) -> None:
        hasher = ScryptPasswordHasher(salt_factory=lambda _size: b"s" * 16)
        hashed = hasher.hash("fictional-bootstrap-password")

        self.assertTrue(hasher.verify("fictional-bootstrap-password", hashed))
        self.assertFalse(hasher.verify("different-fictional-password", hashed))
        self.assertFalse(hasher.needs_rehash(hashed))

    def test_verified_older_safe_policy_requires_rehash(self) -> None:
        old_hasher = ScryptPasswordHasher(salt_factory=lambda _size: b"o" * 16)
        current = old_hasher.hash("fictional-bootstrap-password")
        old_hash = type(current)(
            algorithm=current.algorithm,
            parameters=(("n", 16_384), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=current.salt,
            digest=__import__("hashlib").scrypt(
                b"fictional-bootstrap-password",
                salt=current.salt,
                n=16_384,
                r=8,
                p=1,
                maxmem=64 * 1024 * 1024,
                dklen=32,
            ),
        )

        self.assertTrue(old_hasher.verify("fictional-bootstrap-password", old_hash))
        self.assertTrue(old_hasher.needs_rehash(old_hash))

    def test_database_parameter_order_does_not_trigger_a_rehash(self) -> None:
        hasher = ScryptPasswordHasher()
        current = hasher.hash("fictional-bootstrap-password")
        reordered = type(current)(
            algorithm=current.algorithm, parameters=tuple(reversed(current.parameters)),
            salt=current.salt, digest=current.digest,
        )
        self.assertTrue(hasher.verify("fictional-bootstrap-password", reordered))
        self.assertFalse(hasher.needs_rehash(reordered))


if __name__ == "__main__":
    unittest.main()
