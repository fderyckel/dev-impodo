"""Verify the privileged-account MFA release decision."""

from __future__ import annotations

import unittest

from impodo.domain.identity.mfa import (
    DeploymentExposure,
    PrivilegedAccountKind,
    PrivilegedMfaEnrollment,
    evaluate_privileged_mfa,
)


class PrivilegedMfaDecisionTests(unittest.TestCase):
    def test_loopback_trial_remains_available_without_mfa(self) -> None:
        decision = evaluate_privileged_mfa(
            DeploymentExposure.LOOPBACK_LOCAL_TRIAL,
            PrivilegedAccountKind.PLATFORM_ADMINISTRATOR,
            PrivilegedMfaEnrollment(0, 0),
        )

        self.assertTrue(decision.release_allowed)
        self.assertFalse(decision.phishing_resistant_mfa_required)

    def test_internet_release_requires_webauthn_and_recovery(self) -> None:
        insufficient = (
            PrivilegedMfaEnrollment(0, 20),
            PrivilegedMfaEnrollment(1, 0),
        )
        for enrollment in insufficient:
            with self.subTest(enrollment=enrollment):
                decision = evaluate_privileged_mfa(
                    DeploymentExposure.INTERNET_ACCESSIBLE,
                    PrivilegedAccountKind.ORGANIZATION_ADMINISTRATOR,
                    enrollment,
                )
                self.assertFalse(decision.release_allowed)
                self.assertTrue(decision.phishing_resistant_mfa_required)

        for enrollment in (
            PrivilegedMfaEnrollment(2, 0),
            PrivilegedMfaEnrollment(1, 10),
        ):
            with self.subTest(enrollment=enrollment):
                decision = evaluate_privileged_mfa(
                    DeploymentExposure.INTERNET_ACCESSIBLE,
                    PrivilegedAccountKind.PLATFORM_ADMINISTRATOR,
                    enrollment,
                )
                self.assertTrue(decision.release_allowed)


if __name__ == "__main__":
    unittest.main()
