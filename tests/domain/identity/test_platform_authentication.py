"""Verify secret-safe platform authentication domain facts."""

from __future__ import annotations

import unittest

from impodo.domain.identity.authentication import (
    PlatformAuthenticationCommand,
    PlatformAuthenticationInputError,
    authentication_identifier_digest,
    validate_authentication_digest,
)


class PlatformAuthenticationDomainTests(unittest.TestCase):
    def test_command_repr_does_not_expose_password(self) -> None:
        command = PlatformAuthenticationCommand(
            login_email="Operator@Example.Test",
            password="fictional-login-password",
            source_digest="sha256:" + "a" * 64,
        )

        self.assertNotIn("fictional-login-password", repr(command))

    def test_identifier_digest_is_bounded_and_deterministic(self) -> None:
        digest = authentication_identifier_digest("operator@example.test")

        self.assertEqual(len(digest), 71)
        self.assertEqual(
            digest, authentication_identifier_digest("operator@example.test")
        )
        self.assertNotIn("operator", digest)
        self.assertEqual(validate_authentication_digest(digest), digest)

    def test_source_digest_rejects_raw_or_malformed_values(self) -> None:
        for value in ("127.0.0.1", "sha256:abc", "SHA256:" + "a" * 64):
            with (
                self.subTest(value=value),
                self.assertRaises(PlatformAuthenticationInputError),
            ):
                validate_authentication_digest(value)


if __name__ == "__main__":
    unittest.main()
