"""Verify Odoo-read exceptions map to stable recovery ownership."""

from __future__ import annotations

import unittest

from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.application.odoo_read_failures import (
    OdooReadCredentialMissingError,
    OdooReadFailureCode,
    OdooReadRecoveryKind,
    OdooReadWorkflowError,
    RecoveryOwner,
    classify_odoo_read_failure,
)
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
)
from impodo.application.shared.secrets import SecretStoreError


class OdooReadFailureClassificationTests(unittest.TestCase):
    def test_connector_and_credential_failures_have_distinct_recovery(self):
        cases = (
            (
                OdooReadCredentialMissingError("missing"),
                OdooReadFailureCode.READ_KEY_MISSING,
                OdooReadRecoveryKind.ENTER_READ_KEY,
            ),
            (
                ConnectorAuthenticationError("rejected"),
                OdooReadFailureCode.READ_KEY_REJECTED,
                OdooReadRecoveryKind.REPLACE_READ_KEY,
            ),
            (
                ConnectorAuthorizationError("denied"),
                OdooReadFailureCode.READ_ACCESS_MISSING,
                OdooReadRecoveryKind.USE_KEY_WITH_READ_ACCESS,
            ),
            (
                ConnectorConfigurationError("invalid"),
                OdooReadFailureCode.CONNECTION_DETAILS_INVALID,
                OdooReadRecoveryKind.REVIEW_CONNECTION,
            ),
            (
                ConnectorTransportError("HTTP 503"),
                OdooReadFailureCode.TARGET_UNREACHABLE,
                OdooReadRecoveryKind.RETRY_COMPARISON,
            ),
            (
                ConnectorIncompleteResultError("partial"),
                OdooReadFailureCode.RESPONSE_INCOMPLETE,
                OdooReadRecoveryKind.RETRY_COMPARISON,
            ),
        )

        for error, code, recovery in cases:
            with self.subTest(code=code):
                failure = classify_odoo_read_failure(error)
                self.assertEqual(failure.code, code)
                self.assertEqual(failure.recovery, recovery)

    def test_workflow_code_controls_owner_without_message_parsing(self):
        error = OdooReadWorkflowError(
            OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
            "wording may change",
            support_reference="run-1",
        )

        failure = classify_odoo_read_failure(error)

        self.assertEqual(failure.owner, RecoveryOwner.PREPARE_DATA)
        self.assertEqual(failure.recovery, OdooReadRecoveryKind.PREPARE_AGAIN)
        self.assertEqual(failure.support_reference, "run-1")

    def test_secret_storage_failure_is_not_reported_as_missing_credential(self):
        failure = classify_odoo_read_failure(
            SecretStoreError("Windows credential storage is unavailable")
        )

        self.assertEqual(
            failure.code,
            OdooReadFailureCode.COMPARISON_STORAGE_FAILED,
        )
        self.assertEqual(failure.owner, RecoveryOwner.SUPPORT)

        artifact_failure = classify_odoo_read_failure(
            ArtifactStoreError("comparison publication failed")
        )
        self.assertEqual(
            artifact_failure.code,
            OdooReadFailureCode.COMPARISON_STORAGE_FAILED,
        )


if __name__ == "__main__":
    unittest.main()
