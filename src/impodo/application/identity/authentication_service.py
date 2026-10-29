"""Verify a Platform Administrator without exposing account existence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.identity.authentication_ports import (
    PasswordAuthenticationPolicy,
    PlatformAuthenticationRepository,
)
from impodo.application.identity.recovery_ports import IdentifierDigester
from impodo.domain.identity.authentication import (
    AUTHENTICATION_REJECTED_MESSAGE,
    AuthenticationSecurityEvent,
    AuthenticationThrottlePolicy,
    PlatformAuthenticationCommand,
    PlatformAuthenticationRejectedError,
    PlatformAuthenticationResult,
    authentication_identifier_digest,
    validate_authentication_digest,
)
from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorBootstrapInputError,
    PlatformAdministratorStatus,
    SecurityEventKind,
    normalize_login_email,
)

_DUMMY_PASSWORD_HASH = PasswordHash(
    algorithm="scrypt",
    parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
    salt=b"\x00" * 16,
    digest=b"\x00" * 32,
)


class PlatformAuthenticationService:
    """Apply one generic login policy around bounded persistence calls."""

    def __init__(
        self,
        repository: PlatformAuthenticationRepository,
        password_policy: PasswordAuthenticationPolicy,
        *,
        throttle_policy: AuthenticationThrottlePolicy | None = None,
        identifier_digester: IdentifierDigester | None = None,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._password_policy = password_policy
        self._throttle_policy = throttle_policy or AuthenticationThrottlePolicy()
        self._identifier_digester = identifier_digester
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def authenticate(
        self,
        command: PlatformAuthenticationCommand,
        *,
        deployment_id: str,
    ) -> PlatformAuthenticationResult:
        """Verify one platform credential and upgrade its hash when required."""

        attempted_at = self._clock()
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("authentication clock must return an aware datetime")
        source_digest = validate_authentication_digest(command.source_digest)
        normalized_login_email = _normalize_for_lookup(command.login_email)
        account_digest = (
            self._identifier_digester.digest(
                normalized_login_email,
                scope="authentication-account",
            )
            if self._identifier_digester is not None
            else authentication_identifier_digest(normalized_login_email)
        )
        attempt = self._repository.load_attempt(
            normalized_login_email=normalized_login_email,
            account_digest=account_digest,
            source_digest=source_digest,
            attempted_at=attempted_at,
        )
        credential = attempt.credential
        candidate_hash = (
            credential.password_hash if credential is not None else _DUMMY_PASSWORD_HASH
        )
        verified = self._password_policy.verify(command.password, candidate_hash)
        if (
            attempt.throttled
            or credential is None
            or credential.status is not PlatformAdministratorStatus.ACTIVE
            or not verified
        ):
            self._repository.record_rejection(
                self._event(
                    deployment_id=deployment_id,
                    kind=SecurityEventKind.AUTHENTICATION_REJECTED,
                    subject_administrator_id=(
                        credential.administrator_id if credential is not None else None
                    ),
                    account_digest=account_digest,
                    source_digest=source_digest,
                    occurred_at=attempted_at,
                ),
                count_failure=not attempt.throttled,
                policy=self._throttle_policy,
            )
            raise PlatformAuthenticationRejectedError(AUTHENTICATION_REJECTED_MESSAGE)

        replacement_hash = None
        rehash_event = None
        if self._password_policy.needs_rehash(credential.password_hash):
            replacement_hash = self._password_policy.hash(command.password)
            rehash_event = self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.PASSWORD_REHASHED,
                subject_administrator_id=credential.administrator_id,
                account_digest=account_digest,
                source_digest=source_digest,
                occurred_at=attempted_at,
            )
        password_rehashed = self._repository.record_success(
            self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.AUTHENTICATION_SUCCEEDED,
                subject_administrator_id=credential.administrator_id,
                account_digest=account_digest,
                source_digest=source_digest,
                occurred_at=attempted_at,
            ),
            expected_password_revision=credential.password_revision,
            replacement_hash=replacement_hash,
            rehash_event=rehash_event,
        )
        return PlatformAuthenticationResult(
            administrator_id=credential.administrator_id,
            security_revision=credential.security_revision,
            authenticated_at=attempted_at,
            password_rehashed=password_rehashed,
        )

    def _event(
        self,
        *,
        deployment_id: str,
        kind: SecurityEventKind,
        subject_administrator_id: str | None,
        account_digest: str,
        source_digest: str,
        occurred_at: datetime,
    ) -> AuthenticationSecurityEvent:
        return AuthenticationSecurityEvent(
            event_id=self._identifier_factory(),
            deployment_id=deployment_id,
            kind=kind.value,
            subject_administrator_id=subject_administrator_id,
            principal_hint_digest=account_digest,
            source_digest=source_digest,
            occurred_at=occurred_at,
        )


def _normalize_for_lookup(value: str) -> str:
    try:
        return normalize_login_email(value)
    except PlatformAdministratorBootstrapInputError:
        return ""


__all__ = ["PlatformAuthenticationService"]
