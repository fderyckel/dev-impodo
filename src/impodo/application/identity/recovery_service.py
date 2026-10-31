"""Orchestrate generic reset behavior and one-time token transitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.identity.recovery_ports import (
    AccountRecoveryRepository,
    IdentifierDigester,
    PasswordResetDeliveryError,
    PasswordResetDeliveryPort,
    PasswordResetHasher,
    RecoveryTokenFactory,
)
from impodo.domain.identity.bootstrap import (
    PlatformAdministratorBootstrapInputError,
    SecurityEventKind,
    normalize_login_email,
    validate_bootstrap_password,
)
from impodo.domain.identity.recovery import (
    PASSWORD_RESET_REJECTED_MESSAGE,
    AccountRecoveryInputError,
    AccountRecoveryRejectedError,
    AuthorizedRecoveryTokenCommand,
    AuthorizedRecoveryTokenResult,
    LoginPrincipalKind,
    PasswordResetCommand,
    PasswordResetDelivery,
    PasswordResetMutation,
    PasswordResetRequestCommand,
    PasswordResetRequestResult,
    PasswordResetResult,
    RecoverySecurityEvent,
    RecoveryTokenIssue,
    RecoveryTokenPolicy,
    RecoveryTokenPurpose,
)


class AccountRecoveryService:
    """Keep public reset responses opaque and mutations transaction-bound."""

    def __init__(
        self,
        repository: AccountRecoveryRepository,
        token_factory: RecoveryTokenFactory,
        identifier_digester: IdentifierDigester,
        password_hasher: PasswordResetHasher,
        delivery: PasswordResetDeliveryPort,
        *,
        policy: RecoveryTokenPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._token_factory = token_factory
        self._identifier_digester = identifier_digester
        self._password_hasher = password_hasher
        self._delivery = delivery
        self._policy = policy or RecoveryTokenPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def request_password_reset(
        self,
        command: PasswordResetRequestCommand,
        *,
        deployment_id: str,
    ) -> PasswordResetRequestResult:
        """Accept known and unknown reset requests with the same public result."""

        occurred_at = self._aware_now()
        normalized_login_email = _normalize_for_lookup(command.login_email)
        account_digest = self._identifier_digester.digest(
            normalized_login_email,
            scope="password-reset-account",
        )
        source_digest = self._identifier_digester.digest(
            command.source_digest,
            scope="password-reset-source",
        )
        material = self._token_factory.create(RecoveryTokenPurpose.PASSWORD_RESET)
        issue = RecoveryTokenIssue(
            token_id=material.token_id,
            purpose=RecoveryTokenPurpose.PASSWORD_RESET,
            digest=material.digest,
            issued_at=occurred_at,
            expires_at=(
                occurred_at
                + self._policy.lifetime_for(RecoveryTokenPurpose.PASSWORD_RESET)
            ),
        )
        issued = self._repository.request_password_reset(
            normalized_login_email=normalized_login_email,
            account_digest=account_digest,
            source_digest=source_digest,
            issue=issue,
            request_event=self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.PASSWORD_RESET_REQUESTED,
                subject_administrator_id=None,
                account_digest=account_digest,
                source_digest=source_digest,
                occurred_at=occurred_at,
            ),
            issue_event=self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.RECOVERY_TOKEN_ISSUED,
                subject_administrator_id=None,
                account_digest=account_digest,
                source_digest=source_digest,
                occurred_at=occurred_at,
            ),
            policy=self._policy,
        )
        if issued:
            try:
                self._delivery.deliver(
                    PasswordResetDelivery(
                        normalized_login_email=normalized_login_email,
                        token=material.token,
                        expires_at=issue.expires_at,
                    )
                )
            except PasswordResetDeliveryError:
                pass
        return PasswordResetRequestResult()

    def issue_authorized(
        self,
        command: AuthorizedRecoveryTokenCommand,
        *,
        deployment_id: str,
    ) -> AuthorizedRecoveryTokenResult:
        """Issue an exact-subject link and disclose it once after commit."""

        occurred_at = self._aware_now()
        material = self._token_factory.create(command.purpose)
        issue = RecoveryTokenIssue(
            token_id=material.token_id,
            purpose=command.purpose,
            digest=material.digest,
            issued_at=occurred_at,
            expires_at=occurred_at + self._policy.lifetime_for(command.purpose),
        )
        account_digest = self._identifier_digester.digest(
            command.principal_id,
            scope="authorized-recovery-subject",
        )
        self._repository.issue_authorized(
            command,
            issue,
            self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.RECOVERY_TOKEN_ISSUED,
                subject_administrator_id=(
                    command.principal_id
                    if command.principal_kind.value == "PLATFORM_ADMINISTRATOR"
                    else None
                ),
                account_digest=account_digest,
                source_digest=command.source_digest,
                occurred_at=occurred_at,
            ),
        )
        return AuthorizedRecoveryTokenResult(
            purpose=command.purpose,
            token=material.token,
            expires_at=issue.expires_at,
        )

    def inspect_token_principal(
        self, token: str, purpose: RecoveryTokenPurpose,
    ) -> LoginPrincipalKind | None:
        """Dispatch an issued token cheaply; consumption still validates it atomically."""
        if len(token) > 256:
            return None
        try:
            proof = self._token_factory.prove(token, purpose)
        except AccountRecoveryInputError:
            return None
        return self._repository.inspect_token_principal(
            proof, purpose, observed_at=self._aware_now(),
        )

    def reset_password(
        self,
        command: PasswordResetCommand,
        *,
        deployment_id: str,
    ) -> PasswordResetResult:
        """Consume a reset token with the password and security revision update."""

        occurred_at = self._aware_now()
        try:
            password = validate_bootstrap_password(command.new_password)
            proof = self._token_factory.prove(
                command.token,
                RecoveryTokenPurpose.PASSWORD_RESET,
            )
        except (AccountRecoveryInputError, PlatformAdministratorBootstrapInputError):
            raise AccountRecoveryRejectedError(
                PASSWORD_RESET_REJECTED_MESSAGE
            ) from None
        replacement_hash = self._password_hasher.hash(password)
        source_digest = self._identifier_digester.digest(
            command.source_digest,
            scope="password-reset-source",
        )
        account_digest = self._identifier_digester.digest(
            proof.token_id,
            scope="password-reset-token-selector",
        )
        completed = self._repository.reset_password(
            PasswordResetMutation(
                proof=proof,
                replacement_hash=replacement_hash,
                source_digest=source_digest,
                occurred_at=occurred_at,
            ),
            self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.PASSWORD_RESET_COMPLETED,
                subject_administrator_id=None,
                account_digest=account_digest,
                source_digest=source_digest,
                occurred_at=occurred_at,
            ),
        )
        if not completed:
            raise AccountRecoveryRejectedError(PASSWORD_RESET_REJECTED_MESSAGE)
        return PasswordResetResult(completed_at=occurred_at)

    def _aware_now(self) -> datetime:
        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("account recovery clock must return an aware datetime")
        return occurred_at

    def _event(
        self,
        *,
        deployment_id: str,
        kind: SecurityEventKind,
        subject_administrator_id: str | None,
        account_digest: str,
        source_digest: str,
        occurred_at: datetime,
    ) -> RecoverySecurityEvent:
        return RecoverySecurityEvent(
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


__all__ = ["AccountRecoveryService"]
