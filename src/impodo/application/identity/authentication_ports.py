"""Declare platform password-verification and persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.identity.authentication import (
    AuthenticationSecurityEvent,
    AuthenticationThrottlePolicy,
    PlatformAuthenticationAttempt,
)
from impodo.domain.identity.bootstrap import PasswordHash


class PasswordAuthenticationPolicy(Protocol):
    """Verify and upgrade a slow password hash under the current policy."""

    def hash(self, password: str) -> PasswordHash: ...

    def verify(self, password: str, password_hash: PasswordHash) -> bool: ...

    def needs_rehash(self, password_hash: PasswordHash) -> bool: ...


class PlatformAuthenticationRepository(Protocol):
    """Read and mutate bounded authentication facts in short transactions."""

    def load_attempt(
        self,
        *,
        normalized_login_email: str,
        account_digest: str,
        source_digest: str,
        attempted_at: datetime,
    ) -> PlatformAuthenticationAttempt: ...

    def record_rejection(
        self,
        event: AuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None: ...

    def record_success(
        self,
        success_event: AuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: AuthenticationSecurityEvent | None,
    ) -> bool: ...


__all__ = ["PasswordAuthenticationPolicy", "PlatformAuthenticationRepository"]
