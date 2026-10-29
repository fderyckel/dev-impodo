"""Verify first-operator creation passes one secret-safe atomic bundle."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.application.identity.bootstrap_service import (
    PlatformAdministratorBootstrapService,
)
from impodo.domain.identity.bootstrap import (
    BootstrapSecurityEvent,
    PasswordHash,
    PlatformAdministratorBootstrapCommand,
    PlatformAdministratorBootstrapRecord,
    PlatformAdministratorStatus,
    SecurityEventKind,
)

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)


class _Hasher:
    def __init__(self) -> None:
        self.passwords: list[str] = []

    def hash(self, password: str) -> PasswordHash:
        self.passwords.append(password)
        return PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768),),
            salt=b"0123456789abcdef",
            digest=b"h" * 32,
        )


class _Repository:
    def __init__(self) -> None:
        self.calls: list[
            tuple[PlatformAdministratorBootstrapRecord, BootstrapSecurityEvent]
        ] = []

    def bootstrap(
        self,
        record: PlatformAdministratorBootstrapRecord,
        event: BootstrapSecurityEvent,
    ) -> None:
        self.calls.append((record, event))


class PlatformAdministratorBootstrapServiceTests(unittest.TestCase):
    def test_service_builds_one_active_record_and_one_bounded_event(self) -> None:
        repository = _Repository()
        hasher = _Hasher()
        identifiers = iter(
            (
                "10000000-0000-4000-8000-000000000001",
                "20000000-0000-4000-8000-000000000001",
            )
        )
        service = PlatformAdministratorBootstrapService(
            repository,
            hasher,
            clock=lambda: NOW,
            identifier_factory=lambda: next(identifiers),
        )

        result = service.bootstrap(
            PlatformAdministratorBootstrapCommand(
                login_email=" ADMIN@Example.TEST ",
                display_name=" Platform operator ",
                password="fictional-bootstrap-password",
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(hasher.passwords, ["fictional-bootstrap-password"])
        self.assertEqual(len(repository.calls), 1)
        record, event = repository.calls[0]
        self.assertEqual(record.normalized_login_email, "admin@example.test")
        self.assertEqual(record.display_name, "Platform operator")
        self.assertIs(record.status, PlatformAdministratorStatus.ACTIVE)
        self.assertEqual(record.security_revision, 1)
        self.assertEqual(event.deployment_id, "local-trial")
        self.assertIs(
            event.kind,
            SecurityEventKind.PLATFORM_ADMINISTRATOR_BOOTSTRAPPED,
        )
        self.assertEqual(event.subject_administrator_id, record.administrator_id)
        self.assertEqual(result.administrator_id, record.administrator_id)
        self.assertEqual(result.security_event_id, event.event_id)


if __name__ == "__main__":
    unittest.main()
