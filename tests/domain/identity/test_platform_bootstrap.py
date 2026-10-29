"""Protect first-operator validation and secret-safe value semantics."""

from __future__ import annotations

import unittest

from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorBootstrapCommand,
    PlatformAdministratorBootstrapInputError,
    normalize_login_email,
    validate_bootstrap_display_name,
    validate_bootstrap_password,
)


class PlatformAdministratorBootstrapDomainTests(unittest.TestCase):
    def test_login_email_has_one_stable_global_normalization(self) -> None:
        self.assertEqual(
            normalize_login_email("  ADMIN@Example.TEST  "),
            "admin@example.test",
        )
        self.assertEqual(
            normalize_login_email("ＡＤＭＩＮ@example.test"),
            "admin@example.test",
        )

    def test_invalid_operator_input_fails_without_reproducing_the_value(self) -> None:
        cases = (
            lambda: normalize_login_email("not-an-email"),
            lambda: validate_bootstrap_display_name("   "),
            lambda: validate_bootstrap_password("too-short"),
        )
        for operation in cases:
            with (
                self.subTest(operation=operation),
                self.assertRaises(PlatformAdministratorBootstrapInputError),
            ):
                operation()

    def test_password_values_hide_secret_material_from_representations(self) -> None:
        command = PlatformAdministratorBootstrapCommand(
            login_email="admin@example.test",
            display_name="Platform operator",
            password="fictional-password-value",
        )
        hashed = PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768),),
            salt=b"fictional-salt-1",
            digest=b"d" * 32,
        )

        self.assertNotIn("fictional-password-value", repr(command))
        self.assertNotIn("fictional-salt-1", repr(hashed))
        self.assertNotIn("dddd", repr(hashed))


if __name__ == "__main__":
    unittest.main()
