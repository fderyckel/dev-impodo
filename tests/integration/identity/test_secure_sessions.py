"""Verify high-entropy opaque server-session credential derivation."""

from __future__ import annotations

import unittest

from impodo.adapters.identity.secure_sessions import SecureSessionSecretFactory
from impodo.domain.identity.sessions import ServerSessionInputError


class SecureSessionSecretFactoryTests(unittest.TestCase):
    def test_create_prove_and_rotate_never_expose_raw_secrets_in_repr(self) -> None:
        factory = SecureSessionSecretFactory()
        created = factory.create()
        proof = factory.prove(created.handle, created.csrf_token)
        replacement = factory.rotate(created.session_id)

        self.assertEqual(proof.session_id, created.session_id)
        self.assertEqual(proof.handle_digest, created.handle_digest)
        self.assertEqual(proof.csrf_digest, created.csrf_digest)
        self.assertEqual(replacement.session_id, created.session_id)
        self.assertNotEqual(replacement.handle_digest, created.handle_digest)
        self.assertNotEqual(replacement.csrf_digest, created.csrf_digest)
        for raw_secret in (
            created.handle,
            created.csrf_token,
            replacement.handle,
            replacement.csrf_token,
        ):
            self.assertNotIn(raw_secret, repr(created))
            self.assertNotIn(raw_secret, repr(replacement))

    def test_malformed_or_short_credentials_have_one_bounded_error(self) -> None:
        factory = SecureSessionSecretFactory()
        for handle in ("", "not-a-session", "bad.bad"):
            with self.subTest(handle=handle):
                with self.assertRaisesRegex(
                    ServerSessionInputError,
                    "^Session credentials are invalid$",
                ):
                    factory.prove(handle)


if __name__ == "__main__":
    unittest.main()
