"""Verify opaque reset requests and transaction-bound token consumption."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from impodo.adapters.identity.secure_recovery_tokens import (
    HmacIdentifierDigester,
    SecureRecoveryTokenFactory,
)
from impodo.application.identity.recovery_service import AccountRecoveryService
from impodo.application.identity.recovery_ports import PasswordResetDeliveryError
from impodo.domain.identity.bootstrap import PasswordHash, SecurityEventKind
from impodo.domain.identity.recovery import (
    PASSWORD_RESET_ACCEPTED_MESSAGE,
    PASSWORD_RESET_REJECTED_MESSAGE,
    AccountRecoveryRejectedError,
    AuthorizedRecoveryTokenCommand,
    LoginPrincipalKind,
    PasswordResetCommand,
    PasswordResetDelivery,
    PasswordResetMutation,
    PasswordResetRequestCommand,
    RecoverySecurityEvent,
    RecoveryTokenIssue,
    RecoveryTokenPolicy,
    RecoveryTokenPurpose,
)

NOW = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)
ADMINISTRATOR_ID = "10000000-0000-4000-8000-000000000001"


class _PasswordHasher:
    def hash(self, password: str) -> PasswordHash:
        self.password = password
        return PasswordHash(
            algorithm="scrypt",
            parameters=(("n", 32_768), ("r", 8), ("p", 1), ("dklen", 32)),
            salt=b"s" * 16,
            digest=b"h" * 32,
        )

    def verify(self, password: str, password_hash: PasswordHash) -> bool:
        return False

    def needs_rehash(self, password_hash: PasswordHash) -> bool:
        return False


class _Delivery:
    def __init__(self, *, fails: bool = False) -> None:
        self.deliveries: list[PasswordResetDelivery] = []
        self.fails = fails

    def deliver(self, delivery: PasswordResetDelivery) -> None:
        if self.fails:
            raise PasswordResetDeliveryError("bounded delivery failure")
        self.deliveries.append(delivery)


class _Repository:
    def __init__(self, *, issue_reset: bool = True, complete_reset: bool = True) -> None:
        self.issue_reset = issue_reset
        self.complete_reset = complete_reset
        self.requests: list[dict[str, object]] = []
        self.mutations: list[PasswordResetMutation] = []
        self.authorized: list[AuthorizedRecoveryTokenCommand] = []

    def request_password_reset(self, **kwargs) -> bool:
        self.requests.append(kwargs)
        return self.issue_reset

    def issue_authorized(
        self,
        command: AuthorizedRecoveryTokenCommand,
        issue: RecoveryTokenIssue,
        event: RecoverySecurityEvent,
    ) -> None:
        self.authorized.append(command)
        self.issue = issue
        self.event = event

    def reset_password(
        self,
        mutation: PasswordResetMutation,
        event: RecoverySecurityEvent,
    ) -> bool:
        self.mutations.append(mutation)
        self.reset_event = event
        return self.complete_reset


def _service(
    repository: _Repository,
    delivery: _Delivery,
) -> AccountRecoveryService:
    identifiers = iter(("event-1", "event-2", "event-3"))
    return AccountRecoveryService(
        repository,
        SecureRecoveryTokenFactory(),
        HmacIdentifierDigester(b"deployment-test-key" + b"x" * 32),
        _PasswordHasher(),
        delivery,
        clock=lambda: NOW,
        identifier_factory=lambda: next(identifiers),
    )


class AccountRecoveryServiceTests(unittest.TestCase):
    def test_known_and_unknown_requests_return_the_same_public_result(self) -> None:
        results = []
        deliveries = []
        for issued in (True, False):
            repository = _Repository(issue_reset=issued)
            delivery = _Delivery()
            result = _service(repository, delivery).request_password_reset(
                PasswordResetRequestCommand(
                    login_email="  Operator@Example.Test ",
                    source_digest="raw-source-never-persisted",
                ),
                deployment_id="local-trial",
            )
            results.append(result)
            deliveries.append(delivery)
            request = repository.requests[0]
            self.assertEqual(
                request["normalized_login_email"],
                "operator@example.test",
            )
            self.assertNotIn("operator@example.test", str(request["account_digest"]))
            self.assertNotIn("raw-source", str(request["source_digest"]))
            issue = request["issue"]
            assert isinstance(issue, RecoveryTokenIssue)
            self.assertNotIn(issue.digest.hex(), repr(issue))

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].message, PASSWORD_RESET_ACCEPTED_MESSAGE)
        self.assertEqual(len(deliveries[0].deliveries), 1)
        self.assertFalse(deliveries[1].deliveries)

        failed_delivery_result = _service(
            _Repository(issue_reset=True),
            _Delivery(fails=True),
        ).request_password_reset(
            PasswordResetRequestCommand(
                login_email="operator@example.test",
                source_digest="raw-source-never-persisted",
            ),
            deployment_id="local-trial",
        )
        self.assertEqual(failed_delivery_result, results[0])

    def test_reset_hashes_before_repository_and_uses_one_generic_rejection(self) -> None:
        token = SecureRecoveryTokenFactory().create(
            RecoveryTokenPurpose.PASSWORD_RESET
        ).token
        repository = _Repository(complete_reset=False)

        with self.assertRaisesRegex(
            AccountRecoveryRejectedError,
            f"^{PASSWORD_RESET_REJECTED_MESSAGE}$",
        ):
            _service(repository, _Delivery()).reset_password(
                PasswordResetCommand(
                    token=token,
                    new_password="fictional replacement password",
                    source_digest="raw-source-never-persisted",
                ),
                deployment_id="local-trial",
            )

        self.assertEqual(len(repository.mutations), 1)
        self.assertNotIn("fictional replacement password", repr(repository.mutations[0]))
        self.assertEqual(
            repository.reset_event.kind,
            SecurityEventKind.PASSWORD_RESET_COMPLETED.value,
        )
        for malformed in ("bad-token", token):
            with self.subTest(malformed=malformed):
                command = PasswordResetCommand(
                    token=malformed,
                    new_password=(
                        "short" if malformed == token else "fictional valid password"
                    ),
                    source_digest="source",
                )
                with self.assertRaisesRegex(
                    AccountRecoveryRejectedError,
                    f"^{PASSWORD_RESET_REJECTED_MESSAGE}$",
                ):
                    _service(_Repository(), _Delivery()).reset_password(
                        command,
                        deployment_id="local-trial",
                    )

    def test_authorized_issue_returns_raw_token_only_after_repository_call(self) -> None:
        repository = _Repository()
        result = _service(repository, _Delivery()).issue_authorized(
            AuthorizedRecoveryTokenCommand(
                principal_kind=LoginPrincipalKind.PLATFORM_ADMINISTRATOR,
                principal_id=ADMINISTRATOR_ID,
                purpose=RecoveryTokenPurpose.PASSWORD_RESET,
                source_digest="sha256:" + "c" * 64,
            ),
            deployment_id="local-trial",
        )

        self.assertEqual(repository.authorized[0].principal_id, ADMINISTRATOR_ID)
        self.assertNotIn(result.token, repr(result))
        self.assertEqual(result.expires_at, NOW + RecoveryTokenPolicy().password_reset_lifetime)


if __name__ == "__main__":
    unittest.main()
