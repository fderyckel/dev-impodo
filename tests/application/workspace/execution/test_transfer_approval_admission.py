"""Retained preflight receipts cannot authorize writes after approval retirement."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from impodo.application.workspace.execution.service import ExecutionService
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.workspace.errors import WorkspaceError
from tests.application.workspace import test_scoped_odoo_transfer as scope_fixture


class TransferApprovalAdmissionTests(unittest.TestCase):
    def test_cleared_chain_blocks_direct_start_and_resume_before_journal_or_writer(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                fixture = scope_fixture.ScopedTransferFixture(major, mode)
                snapshot = fixture.compile()
                approved = fixture.workspace
                report = approved.transfer_preflight_report
                self.assertTrue(
                    approved.transfer_preflight_ready(
                        source_selection_hash=report.source_selection_hash,
                        source_schema_hash=report.source_schema_hash,
                    )
                )
                source_hash = fixture.selection.content_hash
                origins = dict(fixture.origins)
                for step in ("destination-matching", "transfer-review", None):
                    with self.subTest(major=major, mode=mode, notice=step):
                        # None covers a later unrelated save losing only the notice.
                        retired = replace(
                            approved,
                            destination_match_plan=None,
                            transfer_order_plan=None,
                            transfer_review_package=None,
                            transfer_review_approval=None,
                            transfer_evidence_recovery_step=step,
                        )
                        self.assertIs(retired.transfer_preflight_report, report)
                        journal, executor = Mock(), Mock()
                        workspace_reader = SimpleNamespace(
                            get=Mock(return_value=retired)
                        )
                        service = ExecutionService(
                            workspace_reader,
                            Mock(),
                            journal,
                            CapabilityAuthorizationPolicy(),
                        )
                        options = {
                            "expected_snapshot_hash": snapshot.semantic_hash,
                            "expected_preflight_hash": report.content_hash,
                            "snapshot": snapshot,
                            "executor": executor,
                            "actor": LOCAL_ACTOR,
                            "read_identity": fixture.identity,
                            "credential_binding_hash": scope_fixture.HASH,
                            "write_identity": Mock(),
                        }
                        with self.assertRaisesRegex(
                            WorkspaceError, "preflight changed"
                        ):
                            service.execute_transfer(retired.workspace_id, **options)
                        with self.assertRaisesRegex(
                            WorkspaceError, "preview or preflight is no longer current"
                        ):
                            service.resume_transfer(
                                retired.workspace_id,
                                expected_execution_run_id="retained-journal",
                                recovery=Mock(),
                                **options,
                            )
                        self.assertEqual(journal.mock_calls, [])
                        self.assertEqual(executor.mock_calls, [])
                        self.assertEqual(fixture.selection.content_hash, source_hash)
                        self.assertEqual(fixture.origins, origins)
                        self.assertIs(
                            fixture.workspace.transfer_review_approval,
                            approved.transfer_review_approval,
                        )
