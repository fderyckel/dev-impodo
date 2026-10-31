"""Verify opaque session creation, access, and elevation rotation."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.adapters.identity.secure_sessions import SecureSessionSecretFactory
from impodo.application.identity.session_service import ServerSessionService
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.sessions import (
    SESSION_REJECTED_MESSAGE,
    AccessServerSessionCommand,
    CreateServerSessionCommand,
    CurrentSessionView,
    RevokeServerSessionCommand,
    RotateServerSessionCommand,
    ServerSessionInputError,
    ServerSessionRecord,
    ServerSessionRejectedError,
    SessionAuthenticationMethod,
    SessionProof,
    SessionSecretMaterial,
    SessionSecurityEvent,
    SessionSubject,
)

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"


class _Repository:
    def __init__(self) -> None:
        self.created: ServerSessionRecord | None = None
        self.events: list[SessionSecurityEvent] = []
        self.resolved: CurrentSessionView | None = None
        self.rotated: CurrentSessionView | None = None
        self.resolve_proof: SessionProof | None = None
        self.replacement: SessionSecretMaterial | None = None

    def create(
        self,
        record: ServerSessionRecord,
        event: SessionSecurityEvent,
    ) -> None:
        self.created = record
        self.events.append(event)

    def resolve(self, **kwargs) -> CurrentSessionView | None:
        self.resolve_proof = kwargs["proof"]
        return self.resolved

    def rotate(self, **kwargs) -> CurrentSessionView | None:
        self.replacement = kwargs["replacement"]
        self.events.append(kwargs["event"])
        return self.rotated

    def revoke(
        self,
        command: RevokeServerSessionCommand,
        event: SessionSecurityEvent,
        *,
        revoked_at: datetime,
    ) -> None:
        self.revocation = (command, revoked_at)
        self.events.append(event)


def _subject() -> SessionSubject:
    return SessionSubject(
        principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
        principal_id=ADMINISTRATOR_ID,
        organization_id=None,
    )


def _service(repository: _Repository) -> ServerSessionService:
    identifiers = iter(
        (
            "20000000-0000-4000-8000-000000000002",
            "30000000-0000-4000-8000-000000000003",
            "40000000-0000-4000-8000-000000000004",
        )
    )
    return ServerSessionService(
        repository,
        SecureSessionSecretFactory(),
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
    )


class ServerSessionServiceTests(unittest.TestCase):
    def test_create_returns_secrets_only_after_fresh_record_is_persisted(self) -> None:
        repository = _Repository()
        credentials = _service(repository).create(
            CreateServerSessionCommand(
                subject=_subject(),
                authentication_method=SessionAuthenticationMethod.PASSWORD,
                security_revision=3,
            ),
            deployment_id="local-trial",
        )

        record = repository.created
        assert record is not None
        self.assertEqual(credentials.session.session_id, record.session_id)
        self.assertNotIn(credentials.handle, repr(credentials))
        self.assertNotIn(credentials.csrf_token, repr(credentials))
        self.assertNotIn(credentials.handle, repr(record))
        self.assertEqual(repository.events[0].kind.value, "SESSION_CREATED")
        self.assertIsNone(record.elevated_until)

    def test_create_accepts_both_tenant_bound_customer_principal_kinds(self) -> None:
        repository = _Repository()
        service = _service(repository)
        organization_subject = SessionSubject(
            principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
            principal_id=ADMINISTRATOR_ID,
            organization_id="acme",
        )

        credentials = service.create(
            CreateServerSessionCommand(
                subject=organization_subject,
                authentication_method=SessionAuthenticationMethod.PASSWORD,
                security_revision=3,
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(credentials.session.subject, organization_subject)
        assert repository.created is not None
        self.assertEqual(repository.created.subject.organization_id, "acme")

        customer = service.create(
                CreateServerSessionCommand(
                    subject=SessionSubject(
                        principal_kind=LoginPrincipalKind.CUSTOMER_USER,
                        principal_id=ADMINISTRATOR_ID,
                        organization_id="acme",
                    ),
                    authentication_method=SessionAuthenticationMethod.PASSWORD,
                    security_revision=3,
                ),
                deployment_id="local-trial",
        )
        self.assertEqual(customer.session.subject.principal_kind, LoginPrincipalKind.CUSTOMER_USER)
        self.assertEqual(customer.session.subject.organization_id, "acme")

    def test_access_requires_matching_handle_and_csrf_without_leaking_reason(self) -> None:
        repository = _Repository()
        service = _service(repository)
        credentials = service.create(
            CreateServerSessionCommand(
                subject=_subject(),
                authentication_method=SessionAuthenticationMethod.PASSWORD,
                security_revision=3,
            ),
            deployment_id="local-trial",
        )
        repository.resolved = credentials.session

        current = service.access(
            AccessServerSessionCommand(
                handle=credentials.handle,
                csrf_token=credentials.csrf_token,
                require_csrf=True,
            )
        )

        self.assertEqual(current.session_id, credentials.session.session_id)
        assert repository.resolve_proof is not None
        self.assertIsNotNone(repository.resolve_proof.csrf_digest)
        for handle in ("malformed", credentials.handle):
            with self.subTest(handle=handle):
                if handle == credentials.handle:
                    repository.resolved = None
                with self.assertRaisesRegex(
                    ServerSessionRejectedError,
                    f"^{SESSION_REJECTED_MESSAGE}$",
                ):
                    service.access(AccessServerSessionCommand(handle=handle))

    def test_privilege_elevation_requires_webauthn_and_rotates_both_secrets(self) -> None:
        repository = _Repository()
        service = _service(repository)
        original = service.create(
            CreateServerSessionCommand(
                subject=_subject(),
                authentication_method=SessionAuthenticationMethod.PASSWORD,
                security_revision=3,
            ),
            deployment_id="local-trial",
        )
        repository.rotated = CurrentSessionView(
            session_id=original.session.session_id,
            subject=_subject(),
            authentication_method=SessionAuthenticationMethod.WEBAUTHN,
            security_revision=3,
            created_at=NOW,
            last_seen_at=NOW,
            idle_expires_at=original.session.idle_expires_at,
            absolute_expires_at=original.session.absolute_expires_at,
            elevated_until=NOW,
            optimistic_revision=2,
        )

        with self.assertRaises(ServerSessionInputError):
            service.rotate_for_elevation(
                RotateServerSessionCommand(
                    handle=original.handle,
                    authentication_method=SessionAuthenticationMethod.PASSWORD,
                    security_revision=3,
                ),
                deployment_id="local-trial",
            )

        rotated = service.rotate_for_elevation(
            RotateServerSessionCommand(
                handle=original.handle,
                authentication_method=SessionAuthenticationMethod.WEBAUTHN,
                security_revision=3,
            ),
            deployment_id="local-trial",
        )

        self.assertNotEqual(rotated.handle, original.handle)
        self.assertNotEqual(rotated.csrf_token, original.csrf_token)
        self.assertEqual(rotated.session.session_id, original.session.session_id)
        self.assertEqual(repository.events[-1].kind.value, "SESSION_ROTATED")


if __name__ == "__main__":
    unittest.main()
