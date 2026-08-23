"""Verify Final review renders credentials only for credential recovery."""

from __future__ import annotations

import unittest

from impodo.application.odoo_read_failures import (
    OdooReadFailure,
    OdooReadFailureCode,
    OdooReadRecoveryKind,
    RecoveryOwner,
)
from impodo.web.presenters.comparison_recovery import comparison_recovery_view


class ComparisonRecoveryPresenterTests(unittest.TestCase):
    def test_only_credential_recoveries_ask_for_a_key(self) -> None:
        credential_codes = {
            OdooReadFailureCode.READ_KEY_MISSING,
            OdooReadFailureCode.READ_KEY_REJECTED,
            OdooReadFailureCode.READ_ACCESS_MISSING,
        }
        recoveries = {
            OdooReadFailureCode.READ_KEY_MISSING:
                OdooReadRecoveryKind.ENTER_READ_KEY,
            OdooReadFailureCode.READ_KEY_REJECTED:
                OdooReadRecoveryKind.REPLACE_READ_KEY,
            OdooReadFailureCode.READ_ACCESS_MISSING:
                OdooReadRecoveryKind.USE_KEY_WITH_READ_ACCESS,
            OdooReadFailureCode.CONNECTION_DETAILS_INVALID:
                OdooReadRecoveryKind.REVIEW_CONNECTION,
            OdooReadFailureCode.TARGET_UNREACHABLE:
                OdooReadRecoveryKind.RETRY_COMPARISON,
            OdooReadFailureCode.RESPONSE_INCOMPLETE:
                OdooReadRecoveryKind.RETRY_COMPARISON,
            OdooReadFailureCode.SCHEMA_EVIDENCE_MISSING:
                OdooReadRecoveryKind.CAPTURE_ODOO_DATA,
            OdooReadFailureCode.SCHEMA_EVIDENCE_STALE:
                OdooReadRecoveryKind.REFRESH_ODOO_DATA,
            OdooReadFailureCode.REFERENCE_POLICY_MISMATCH:
                OdooReadRecoveryKind.REVIEW_FIELD_MATCH,
            OdooReadFailureCode.MAPPING_EVIDENCE_STALE:
                OdooReadRecoveryKind.REVIEW_FIELD_MATCH,
            OdooReadFailureCode.PREPARED_EVIDENCE_STALE:
                OdooReadRecoveryKind.PREPARE_AGAIN,
            OdooReadFailureCode.LOCAL_PROFILE_REQUIRED:
                OdooReadRecoveryKind.RECONNECT_LOCAL_ODOO,
            OdooReadFailureCode.COMPARISON_STORAGE_FAILED:
                OdooReadRecoveryKind.VIEW_SUPPORT_DETAILS,
            OdooReadFailureCode.UNEXPECTED_COMPARISON_FAILURE:
                OdooReadRecoveryKind.VIEW_SUPPORT_DETAILS,
        }

        for code, recovery in recoveries.items():
            with self.subTest(code=code):
                view = comparison_recovery_view(
                    "project-1",
                    OdooReadFailure(
                        code=code,
                        owner=RecoveryOwner.SUPPORT,
                        recovery=recovery,
                        support_code=code.value,
                    ),
                )
                self.assertEqual(
                    view.asks_for_credential,
                    code in credential_codes,
                )

    def test_schema_mapping_and_preparation_actions_return_to_owners(self) -> None:
        cases = (
            (
                OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                OdooReadRecoveryKind.REFRESH_ODOO_DATA,
                "/workspaces/project-1/schema",
            ),
            (
                OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                OdooReadRecoveryKind.REVIEW_FIELD_MATCH,
                "/workspaces/project-1/mapping",
            ),
            (
                OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                OdooReadRecoveryKind.PREPARE_AGAIN,
                "/workspaces/project-1/prepare",
            ),
        )

        for code, recovery, href in cases:
            with self.subTest(code=code):
                view = comparison_recovery_view(
                    "project-1",
                    OdooReadFailure(
                        code=code,
                        owner=RecoveryOwner.SUPPORT,
                        recovery=recovery,
                        support_code=code.value,
                    ),
                )
                self.assertEqual(view.action_href, href)
                self.assertFalse(view.asks_for_credential)


if __name__ == "__main__":
    unittest.main()
