"""Orchestrate opaque server-side sessions and credential rotation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.identity.session_ports import (
    ServerSessionRepository,
    SessionSecretFactory,
)
from impodo.domain.identity.bootstrap import SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    SESSION_REJECTED_MESSAGE,
    AccessServerSessionCommand,
    CreateServerSessionCommand,
    CurrentSessionView,
    RevokeServerSessionCommand,
    RotateServerSessionCommand,
    ServerSessionCredentials,
    ServerSessionInputError,
    ServerSessionPolicy,
    ServerSessionRecord,
    ServerSessionRejectedError,
    SessionAuthenticationMethod,
    SessionSecurityEvent,
    SessionSubject,
)


class ServerSessionService:
    """Keep raw credentials outside persistence and rotate them after elevation."""

    def __init__(
        self,
        repository: ServerSessionRepository,
        secret_factory: SessionSecretFactory,
        *,
        policy: ServerSessionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._secret_factory = secret_factory
        self._policy = policy or ServerSessionPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def create(
        self,
        command: CreateServerSessionCommand,
        *,
        deployment_id: str,
    ) -> ServerSessionCredentials:
        """Create fresh credentials without accepting an anonymous browser handle."""

        if command.subject.principal_kind not in {
            LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
            LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
            LoginPrincipalKind.CUSTOMER_USER,
        }:
            raise ServerSessionInputError("Session subject is not supported")
        created_at = self._aware_now()
        material = self._secret_factory.create()
        absolute_expires_at = created_at + self._policy.absolute_timeout
        record = ServerSessionRecord(
            session_id=material.session_id,
            subject=command.subject,
            authentication_method=command.authentication_method,
            security_revision=command.security_revision,
            handle_digest=material.handle_digest,
            csrf_digest=material.csrf_digest,
            created_at=created_at,
            last_seen_at=created_at,
            idle_expires_at=min(
                created_at + self._policy.idle_timeout,
                absolute_expires_at,
            ),
            absolute_expires_at=absolute_expires_at,
            elevated_until=None,
            optimistic_revision=1,
        )
        self._repository.create(
            record,
            self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.SESSION_CREATED,
                subject=command.subject,
                actor=None,
                occurred_at=created_at,
            ),
        )
        return ServerSessionCredentials(
            handle=material.handle,
            csrf_token=material.csrf_token,
            session=_view(record),
        )

    def access(self, command: AccessServerSessionCommand) -> CurrentSessionView:
        """Resolve and touch one session, rejecting every unavailable state alike."""

        accessed_at = self._aware_now()
        try:
            proof = self._secret_factory.prove(
                command.handle,
                command.csrf_token if command.require_csrf else None,
            )
        except ServerSessionInputError:
            raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE) from None
        session = self._repository.resolve(
            proof=proof,
            accessed_at=accessed_at,
            proposed_idle_expires_at=accessed_at + self._policy.idle_timeout,
        )
        if session is None:
            raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE)
        return session

    def rotate_for_elevation(
        self,
        command: RotateServerSessionCommand,
        *,
        deployment_id: str,
    ) -> ServerSessionCredentials:
        """Rotate both browser secrets after phishing-resistant elevation."""

        if command.authentication_method is not SessionAuthenticationMethod.WEBAUTHN:
            raise ServerSessionInputError("Privilege elevation requires WebAuthn")
        rotated_at = self._aware_now()
        try:
            proof = self._secret_factory.prove(command.handle)
            replacement = self._secret_factory.rotate(proof.session_id)
        except ServerSessionInputError:
            raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE) from None
        session = self._repository.rotate(
            proof=proof,
            replacement=replacement,
            authentication_method=command.authentication_method,
            expected_security_revision=command.security_revision,
            rotated_at=rotated_at,
            proposed_idle_expires_at=rotated_at + self._policy.idle_timeout,
            elevated_until=rotated_at + self._policy.elevation_timeout,
            event=self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.SESSION_ROTATED,
                subject=None,
                actor=None,
                occurred_at=rotated_at,
            ),
        )
        if session is None:
            raise ServerSessionRejectedError(SESSION_REJECTED_MESSAGE)
        return ServerSessionCredentials(
            handle=replacement.handle,
            csrf_token=replacement.csrf_token,
            session=session,
        )

    def revoke(
        self,
        command: RevokeServerSessionCommand,
        *,
        deployment_id: str,
    ) -> None:
        """Revoke one exact current session under optimistic concurrency."""

        revoked_at = self._aware_now()
        self._repository.revoke(
            command,
            self._event(
                deployment_id=deployment_id,
                kind=SecurityEventKind.SESSION_REVOKED,
                subject=None,
                actor=command.actor,
                occurred_at=revoked_at,
            ),
            revoked_at=revoked_at,
        )

    def _aware_now(self) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("server session clock must return an aware datetime")
        return observed_at

    def _event(
        self,
        *,
        deployment_id: str,
        kind: SecurityEventKind,
        subject: SessionSubject | None,
        actor: SessionSubject | None,
        occurred_at: datetime,
    ) -> SessionSecurityEvent:
        return SessionSecurityEvent(
            event_id=self._identifier_factory(),
            deployment_id=deployment_id,
            kind=kind,
            subject=subject,
            actor=actor,
            occurred_at=occurred_at,
        )


def _view(record: ServerSessionRecord) -> CurrentSessionView:
    return CurrentSessionView(
        session_id=record.session_id,
        subject=record.subject,
        authentication_method=record.authentication_method,
        security_revision=record.security_revision,
        created_at=record.created_at,
        last_seen_at=record.last_seen_at,
        idle_expires_at=record.idle_expires_at,
        absolute_expires_at=record.absolute_expires_at,
        elevated_until=record.elevated_until,
        optimistic_revision=record.optimistic_revision,
    )


__all__ = ["ServerSessionService"]
