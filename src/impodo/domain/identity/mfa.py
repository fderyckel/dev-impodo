"""Record the privileged-account MFA release decision."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DeploymentExposure(StrEnum):
    """Distinguish the bounded local trial from Internet exposure."""

    LOOPBACK_LOCAL_TRIAL = "LOOPBACK_LOCAL_TRIAL"
    INTERNET_ACCESSIBLE = "INTERNET_ACCESSIBLE"


class PrivilegedAccountKind(StrEnum):
    """Name accounts that can change cross-tenant or tenant policy."""

    PLATFORM_ADMINISTRATOR = "PLATFORM_ADMINISTRATOR"
    ORGANIZATION_ADMINISTRATOR = "ORGANIZATION_ADMINISTRATOR"


@dataclass(frozen=True, slots=True)
class PrivilegedMfaEnrollment:
    """Describe phishing-resistant authenticators and offline recovery."""

    webauthn_credential_count: int
    unused_recovery_code_count: int

    def __post_init__(self) -> None:
        if self.webauthn_credential_count < 0 or self.unused_recovery_code_count < 0:
            raise ValueError("MFA enrollment counts cannot be negative")


@dataclass(frozen=True, slots=True)
class PrivilegedMfaDecision:
    """State whether the deployment may pass its privileged-access gate."""

    release_allowed: bool
    phishing_resistant_mfa_required: bool
    reason: str


def evaluate_privileged_mfa(
    exposure: DeploymentExposure,
    account_kind: PrivilegedAccountKind,
    enrollment: PrivilegedMfaEnrollment,
) -> PrivilegedMfaDecision:
    """Apply the release-blocking MFA decision for one privileged account."""

    if exposure is DeploymentExposure.LOOPBACK_LOCAL_TRIAL:
        return PrivilegedMfaDecision(
            release_allowed=True,
            phishing_resistant_mfa_required=False,
            reason="Loopback-only local trial does not require privileged MFA",
        )
    has_resilient_enrollment = enrollment.webauthn_credential_count >= 2 or (
        enrollment.webauthn_credential_count >= 1
        and enrollment.unused_recovery_code_count > 0
    )
    if has_resilient_enrollment:
        return PrivilegedMfaDecision(
            release_allowed=True,
            phishing_resistant_mfa_required=True,
            reason=(
                f"{account_kind.value} has phishing-resistant MFA and a recovery path"
            ),
        )
    return PrivilegedMfaDecision(
        release_allowed=False,
        phishing_resistant_mfa_required=True,
        reason=(
            f"{account_kind.value} requires WebAuthn plus a second authenticator "
            "or unused offline recovery codes before Internet release"
        ),
    )


__all__ = [
    "DeploymentExposure",
    "PrivilegedAccountKind",
    "PrivilegedMfaDecision",
    "PrivilegedMfaEnrollment",
    "evaluate_privileged_mfa",
]
