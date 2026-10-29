"""Verify opaque, purpose-bound recovery tokens and keyed identifier digests."""

from __future__ import annotations

import unittest

from impodo.adapters.identity.secure_recovery_tokens import (
    HmacIdentifierDigester,
    SecureRecoveryTokenFactory,
)
from impodo.domain.identity.recovery import (
    AccountRecoveryInputError,
    RecoveryTokenPurpose,
)


class SecureRecoveryTokenTests(unittest.TestCase):
    def test_round_trip_is_purpose_bound_and_secret_safe_in_repr(self) -> None:
        factory = SecureRecoveryTokenFactory()
        material = factory.create(RecoveryTokenPurpose.PASSWORD_RESET)

        proof = factory.prove(
            material.token,
            RecoveryTokenPurpose.PASSWORD_RESET,
        )

        self.assertEqual(proof.token_id, material.token_id)
        self.assertEqual(proof.digest, material.digest)
        self.assertNotIn(material.token, repr(material))
        self.assertNotIn(material.digest.hex(), repr(material))
        self.assertNotIn(material.digest.hex(), repr(proof))
        activation_proof = factory.prove(
            material.token,
            RecoveryTokenPurpose.ACTIVATION,
        )
        self.assertNotEqual(activation_proof.digest, proof.digest)

    def test_malformed_tokens_share_one_bounded_input_error(self) -> None:
        factory = SecureRecoveryTokenFactory()
        for value in ("", "missing-selector", "not-a-uuid.secret", "000.secret"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    AccountRecoveryInputError,
                    "^Recovery token is invalid$",
                ):
                    factory.prove(value, RecoveryTokenPurpose.PASSWORD_RESET)

    def test_identifier_digest_is_keyed_and_scope_separated(self) -> None:
        first = HmacIdentifierDigester(b"a" * 32)
        second = HmacIdentifierDigester(b"b" * 32)

        account = first.digest("operator@example.test", scope="account")

        self.assertRegex(account, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(
            account,
            first.digest("operator@example.test", scope="source"),
        )
        self.assertNotEqual(
            account,
            second.digest("operator@example.test", scope="account"),
        )


if __name__ == "__main__":
    unittest.main()
