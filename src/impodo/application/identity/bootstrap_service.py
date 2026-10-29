"""Create the first built-in Platform Administrator once."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.identity.bootstrap_ports import (
    PasswordHasher,
    PlatformAdministratorBootstrapRepository,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PlatformAdministratorBootstrapCommand,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorBootstrapResult,
    PlatformAdministratorStatus,
    SecurityEventKind,
    normalize_login_email,
    validate_bootstrap_display_name,
    validate_bootstrap_password,
)


class PlatformAdministratorBootstrapService:
    """Validate and atomically persist the first active platform operator."""

    def __init__(
        self,
        repository: PlatformAdministratorBootstrapRepository,
        password_hasher: PasswordHasher,
        *,
        clock: Callable[[], datetime] | None = None,
        identifier_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier_factory = identifier_factory or (lambda: str(uuid4()))

    def bootstrap(
        self,
        command: PlatformAdministratorBootstrapCommand,
        *,
        deployment_id: str,
    ) -> PlatformAdministratorBootstrapResult:
        """Create one active operator and append one bounded security event."""

        normalized_login_email = normalize_login_email(command.login_email)
        display_name = validate_bootstrap_display_name(command.display_name)
        password = validate_bootstrap_password(command.password)
        occurred_at = self._clock()
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("bootstrap clock must return an aware datetime")
        administrator_id = self._identifier_factory()
        security_event_id = self._identifier_factory()
        record = PlatformAdministratorBootstrapRecord(
            administrator_id=administrator_id,
            normalized_login_email=normalized_login_email,
            display_name=display_name,
            status=PlatformAdministratorStatus.ACTIVE,
            security_revision=1,
            password_hash=self._password_hasher.hash(password),
            created_at=occurred_at,
        )
        event = BootstrapSecurityEvent(
            event_id=security_event_id,
            deployment_id=deployment_id,
            kind=SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
            subject_administrator_id=administrator_id,
            occurred_at=occurred_at,
        )
        self._repository.bootstrap(record, event)
        return PlatformAdministratorBootstrapResult(
            administrator_id=administrator_id,
            security_event_id=security_event_id,
            created_at=occurred_at,
        )


__all__ = ["PlatformAdministratorBootstrapService"]
