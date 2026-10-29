"""Verify generic login behavior, throttling, and policy rehash orchestration."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.adapters.identity.secure_recovery_tokens import HmacIdentifierDigester
from impodo.application.identity.authentication_service import (
    PlatformAuthenticationService,
)
from impodo.domain.identity.authentication import (
    AUTHENTICATION_REJECTED_MESSAGE,
    AuthenticationSecurityEvent,
    AuthenticationThrottlePolicy,
    PlatformAuthenticationAttempt,
    PlatformAuthenticationCommand,
    PlatformAuthenticationCredential,
    PlatformAuthenticationRejectedError,
    authentication_identifier_digest,
)
from impodo.domain.identity.bootstrap import (
    PasswordHash,
    PlatformAdministratorStatus,
    SecurityEventKind,
)

NOW = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
SOURCE_DIGEST = "sha256:" + "b" * 64
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"


def _password_hash(*, cost: int = 32_768, digest: bytes = b"h" * 32) -> PasswordHash:
    return PasswordHash(
        algorithm="scrypt",
        parameters=(("n", cost), ("r", 8), ("p", 1), ("dklen", 32)),
        salt=b"s" * 16,
        digest=digest,
    )


def _credential(
    *,
    status: PlatformAdministratorStatus = PlatformAdministratorStatus.ACTIVE,
    cost: int = 32_768,
) -> PlatformAuthenticationCredential:
    return PlatformAuthenticationCredential(
        administrator_id=ADMINISTRATOR_ID,
        status=status,
        security_revision=3,
        password_hash=_password_hash(cost=cost),
        password_revision=2,
    )


class _PasswordPolicy:
    def __init__(self, *, verified: bool, rehash: bool = False) -> None:
        self.verified = verified
        self.rehash = rehash
        self.verified_hashes: list[PasswordHash] = []
        self.hashed_passwords: list[str] = []

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        self.assert_secret = password
        self.verified_hashes.append(password_hash)
        return self.verified

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        return self.rehash

    def hash(self, password: str) -> PasswordHash:
        self.hashed_passwords.append(password)
        return _password_hash(digest=b"r" * 32)


class _Repository:
    def __init__(self, attempt: PlatformAuthenticationAttempt) -> None:
        self.attempt = attempt
        self.loads: list[dict[str, object]] = []
        self.rejections: list[tuple[AuthenticationSecurityEvent, bool]] = []
        self.successes: list[
            tuple[
                AuthenticationSecurityEvent,
                int,
                PasswordHash | None,
                AuthenticationSecurityEvent | None,
            ]
        ] = []

    def load_attempt(self, **kwargs) -> PlatformAuthenticationAttempt:
        self.loads.append(kwargs)
        return self.attempt

    def record_rejection(
        self,
        event: AuthenticationSecurityEvent,
        *,
        count_failure: bool,
        policy: AuthenticationThrottlePolicy,
    ) -> None:
        self.policy = policy
        self.rejections.append((event, count_failure))

    def record_success(
        self,
        success_event: AuthenticationSecurityEvent,
        *,
        expected_password_revision: int,
        replacement_hash: PasswordHash | None,
        rehash_event: AuthenticationSecurityEvent | None,
    ) -> bool:
        self.successes.append(
            (
                success_event,
                expected_password_revision,
                replacement_hash,
                rehash_event,
            )
        )
        return replacement_hash is not None


def _attempt(
    credential: PlatformAuthenticationCredential | None,
    *,
    throttled: bool = False,
) -> PlatformAuthenticationAttempt:
    return PlatformAuthenticationAttempt(
        account_digest=authentication_identifier_digest("operator@example.test"),
        source_digest=SOURCE_DIGEST,
        credential=credential,
        throttled=throttled,
    )


def _service(
    repository: _Repository, policy: _PasswordPolicy
) -> PlatformAuthenticationService:
    identifiers = iter(("event-1", "event-2", "event-3"))
    return PlatformAuthenticationService(
        repository,
        policy,
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
    )


class PlatformAuthenticationServiceTests(unittest.TestCase):
    def test_success_normalizes_email_and_returns_session_principal_facts(self) -> None:
        repository = _Repository(_attempt(_credential()))
        policy = _PasswordPolicy(verified=True)

        result = _service(repository, policy).authenticate(
            PlatformAuthenticationCommand(
                login_email="  Operator@Example.Test ",
                password="fictional-login-password",
                source_digest=SOURCE_DIGEST,
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(result.administrator_id, ADMINISTRATOR_ID)
        self.assertEqual(result.security_revision, 3)
        self.assertFalse(result.password_rehashed)
        self.assertEqual(
            repository.loads[0]["normalized_login_email"], "operator@example.test"
        )
        self.assertEqual(repository.successes[0][1], 2)
        self.assertEqual(
            repository.successes[0][0].kind,
            SecurityEventKind.AUTHENTICATION_SUCCEEDED.value,
        )
        self.assertFalse(repository.rejections)

    def test_success_rehashes_with_compare_and_swap_metadata(self) -> None:
        repository = _Repository(_attempt(_credential(cost=16_384)))
        policy = _PasswordPolicy(verified=True, rehash=True)

        result = _service(repository, policy).authenticate(
            PlatformAuthenticationCommand(
                login_email="operator@example.test",
                password="fictional-login-password",
                source_digest=SOURCE_DIGEST,
            ),
            deployment_id="local-trial",
        )

        self.assertTrue(result.password_rehashed)
        self.assertEqual(policy.hashed_passwords, ["fictional-login-password"])
        replacement = repository.successes[0][2]
        rehash_event = repository.successes[0][3]
        self.assertIsNotNone(replacement)
        self.assertIsNotNone(rehash_event)
        assert rehash_event is not None
        self.assertEqual(rehash_event.kind, SecurityEventKind.PASSWORD_REHASHED.value)

    def test_browser_composition_can_key_the_low_entropy_account_digest(self) -> None:
        repository = _Repository(_attempt(_credential()))
        digester = HmacIdentifierDigester(b"deployment-key" + b"x" * 32)
        service = PlatformAuthenticationService(
            repository,
            _PasswordPolicy(verified=True),
            identifier_digester=digester,
            clock=lambda: NOW,
            identifier_factory=lambda: "event-1",
        )

        service.authenticate(
            PlatformAuthenticationCommand(
                login_email="operator@example.test",
                password="fictional-login-password",
                source_digest=SOURCE_DIGEST,
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(
            repository.loads[0]["account_digest"],
            digester.digest(
                "operator@example.test",
                scope="authentication-account",
            ),
        )

    def test_unknown_wrong_inactive_and_invalid_identifier_share_one_response(
        self,
    ) -> None:
        cases = (
            ("unknown@example.test", _attempt(None), True),
            ("operator@example.test", _attempt(_credential()), False),
            (
                "operator@example.test",
                _attempt(_credential(status=PlatformAdministratorStatus.DEACTIVATED)),
                True,
            ),
            ("not-an-email", _attempt(None), True),
        )
        for login_email, attempt, verified in cases:
            with self.subTest(login_email=login_email, credential=attempt.credential):
                repository = _Repository(attempt)
                policy = _PasswordPolicy(verified=verified)

                with self.assertRaisesRegex(
                    PlatformAuthenticationRejectedError,
                    f"^{AUTHENTICATION_REJECTED_MESSAGE}$",
                ):
                    _service(repository, policy).authenticate(
                        PlatformAuthenticationCommand(
                            login_email=login_email,
                            password="fictional-login-password",
                            source_digest=SOURCE_DIGEST,
                        ),
                        deployment_id="local-trial",
                    )

                self.assertEqual(len(policy.verified_hashes), 1)
                self.assertEqual(len(repository.rejections), 1)
                event, count_failure = repository.rejections[0]
                self.assertTrue(count_failure)
                self.assertEqual(
                    event.kind,
                    SecurityEventKind.AUTHENTICATION_REJECTED.value,
                )
                self.assertNotIn(login_email, repr(event))

    def test_locked_attempt_records_event_without_extending_failure_count(self) -> None:
        repository = _Repository(_attempt(_credential(), throttled=True))

        with self.assertRaises(PlatformAuthenticationRejectedError):
            _service(repository, _PasswordPolicy(verified=True)).authenticate(
                PlatformAuthenticationCommand(
                    login_email="operator@example.test",
                    password="fictional-login-password",
                    source_digest=SOURCE_DIGEST,
                ),
                deployment_id="local-trial",
            )

        self.assertFalse(repository.rejections[0][1])


if __name__ == "__main__":
    unittest.main()
