"""Declare generic shared-server authentication persistence ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.identity.authentication import AuthenticationThrottlePolicy
from impodo.domain.identity.bootstrap import PasswordHash
from impodo.domain.identity.server_authentication import (
    ServerAuthenticationAttempt,
    ServerAuthenticationSecurityEvent,
)


class ServerAuthenticationRepository(Protocol):
    """Read and mutate global login facts in short transactions."""

    def load_attempt(
        self,
        *,
        normalized_login_email: str,
        account_digest: str,
        source_digest: str,
        attempted_at: datetime,
    ) -> ServerAuthenticationAttempt: ...

    def record_rejection(
        self,
        event: ServerAuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None: ...

    def record_success(
        self,
        success_event: ServerAuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: ServerAuthenticationSecurityEvent | None,
    ) -> bool: ...


__all__ = ["ServerAuthenticationRepository"]
