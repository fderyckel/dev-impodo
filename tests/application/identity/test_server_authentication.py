"""Verify generic Platform and Organization Administrator login orchestration."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.adapters.identity.secure_recovery_tokens import HmacIdentifierDigester
from impodo.application.identity.server_authentication_service import (
    ServerAuthenticationService,
)
from impodo.domain.identity.authentication import (
    AuthenticationThrottlePolicy,
    authentication_identifier_digest,
)
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind
from impodo.domain.identity.recovery import LoginPrincipalKind
from impodo.domain.identity.server_authentication import (
    SERVER_AUTHENTICATION_REJECTED_MESSAGE,
    ServerAuthenticationAttempt,
    ServerAuthenticationCommand,
    ServerAuthenticationCredential,
    ServerAuthenticationRejectedError,
    ServerAuthenticationSecurityEvent,
)

NOW = datetime(2026, 10, 29, 21, 0, tzinfo=UTC)
SOURCE_DIGEST = "sha256:" + "b" * 64
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"


def _password_hash() -> PasswordHash:
    return PasswordHash(
        algorithm="scrypt",
        parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
        salt=b"s" * 16,
        digest=b"h" * 32,
    )


def _credential(*, eligible: bool = True) -> ServerAuthenticationCredential:
    return ServerAuthenticationCredential(
        principal_kind=LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR,
        principal_id=ADMINISTRATOR_ID,
        organization_id="acme",
        eligible=eligible,
        security_revision=3,
        password_hash=_password_hash(),
        password_revision=2,
    )


class _PasswordPolicy:
    def __init__(self, *, verified: bool) -> None:
        self.verified = verified
        self.verifications = 0

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        self.verifications += 1
        return self.verified

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        return False

    def hash(self, password: str) -> PasswordHash:
        return _password_hash()


class _Repository:
    def __init__(self, credential: ServerAuthenticationCredential | None) -> None:
        self.attempt = ServerAuthenticationAttempt(
            account_digest=authentication_identifier_digest("admin@acme.test"),
            source_digest=SOURCE_DIGEST,
            credential=credential,
            throttled=False,
        )
        self.loads: list[dict[str, object]] = []
        self.rejections: list[ServerAuthenticationSecurityEvent] = []
        self.successes: list[ServerAuthenticationSecurityEvent] = []

    def load_attempt(self, **kwargs) -> ServerAuthenticationAttempt:
        self.loads.append(kwargs)
        return self.attempt

    def record_rejection(
        self,
        event: ServerAuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None:
        self.rejections.append(event)

    def record_success(
        self,
        success_event: ServerAuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: ServerAuthenticationSecurityEvent | None,
    ) -> bool:
        self.successes.append(success_event)
        return False


def _service(repository: _Repository, policy: _PasswordPolicy) -> ServerAuthenticationService:
    identifiers = iter(("event-1", "event-2"))
    return ServerAuthenticationService(
        repository,
        policy,
        identifier_digester=HmacIdentifierDigester(b"deployment-key" + b"x" * 32),
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
    )


class ServerAuthenticationServiceTests(unittest.TestCase):
    def test_organization_administrator_success_returns_tenant_binding(self) -> None:
        repository = _Repository(_credential())
        result = _service(repository, _PasswordPolicy(verified=True)).authenticate(
            ServerAuthenticationCommand(
                login_email=" Admin@Acme.Test ",
                password="fictional-password",
                source_digest=SOURCE_DIGEST,
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(result.principal_kind, LoginPrincipalKind.ORGANIZATION_ADMINISTRATOR)
        self.assertEqual(result.organization_id, "acme")
        self.assertEqual(repository.loads[0]["normalized_login_email"], "admin@acme.test")
        self.assertEqual(repository.successes[0].kind, SecurityEventKind.AUTHENTICATION_SUCCEEDED)

    def test_unknown_wrong_and_ineligible_each_verify_once_and_reject_generically(self) -> None:
        for credential, verified in ((None, True), (_credential(), False), (_credential(eligible=False), True)):
            with self.subTest(credential=credential, verified=verified):
                repository = _Repository(credential)
                policy = _PasswordPolicy(verified=verified)
                with self.assertRaisesRegex(
                    ServerAuthenticationRejectedError,
                    f"^{SERVER_AUTHENTICATION_REJECTED_MESSAGE}$",
                ):
                    _service(repository, policy).authenticate(
                        ServerAuthenticationCommand(
                            login_email="admin@acme.test",
                            password="fictional-password",
                            source_digest=SOURCE_DIGEST,
                        ),
                        deployment_id="local-trial",
                    )
                self.assertEqual(policy.verifications, 1)
                self.assertEqual(repository.rejections[0].kind, SecurityEventKind.AUTHENTICATION_REJECTED)


if __name__ == "__main__":
    unittest.main()
