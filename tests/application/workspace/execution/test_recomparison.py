from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.workspace.execution.service import ExecutionPreview, ExecutionService, execution_api_scope
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus, ExecutionRun, ExecutionRunStatus
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity, target_record_binding_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceState
from tests.application.workspace.execution.test_service import HASH, _snapshot


class FreshRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.original = replace(_snapshot(), rows=_snapshot().rows[:2],
                                counts={'CREATE': 2, 'UPDATE': 0, 'UNCHANGED': 0})
        self.fresh = replace(self.original, preflight_run_id=str(uuid4()),
            rows=(replace(self.original.rows[0], disposition='UNCHANGED', target_match_count=1,
                          target_binding_hash=target_record_binding_hash('product.category', 71), fields=()),
                  self.original.rows[1]), counts={'CREATE': 1, 'UPDATE': 0, 'UNCHANGED': 1},
            read_credential_binding_hash=HASH, read_principal_hash=HASH,
            read_permission_hash=HASH, read_context_hash=HASH,
            readable_models=tuple(m.model for m in execution_api_scope(self.original).models))
        self.run = ExecutionRun(str(uuid4()), self.original.workspace_id, self.original.semantic_hash,
            self.original.root_hash, self.original.preflight_run_id, self.original.target_hash,
            self.original.target_database, 10, ExecutionRunStatus.RUNNING, datetime.now(timezone.utc),
            'Operator', None, tuple(ExecutionRowAttempt(row.row_id, row.dataset, row.source_row,
                row.target_model, 'CREATE', tuple(i.field for i in row.fields), row.proposed_external_id,
                status=ExecutionRowStatus.COMMITTED if index == 0 else ExecutionRowStatus.IN_FLIGHT,
                attempt=1, odoo_id=71 if index == 0 else None)
                for index, row in enumerate(self.original.rows)), HASH, HASH, HASH, HASH)
        self.journal = Mock()
        self.journal.get_current_run.return_value = self.run
        self.preflight = Mock()
        self.preflight.execution_snapshot.return_value = self.original
        self.service = ExecutionService(SimpleNamespace(get=lambda _: WorkspaceState(
            workspace_id=self.original.workspace_id, name='Recovery', source_system='CSV')),
            self.preflight, self.journal, CapabilityAuthorizationPolicy())
        self.preview = ExecutionPreview(self.fresh, (), None, execution_api_scope(self.fresh), 0)
        self.service.current_preview = Mock(return_value=self.preview)
        scope = self.preview.api_scope
        self.read = OdooReadIdentity(self.fresh.target_hash, HASH, HASH, HASH,
                                    self.fresh.readable_models, '2026-09-15T12:00:00Z')
        self.write = OdooWriteIdentity(self.fresh.target_hash, HASH, HASH, HASH,
            tuple(m.model for m in scope.models), tuple(m.model for m in scope.models if m.write_fields),
            '2026-09-15T12:00:00Z')

    def close(self, **overrides):
        arguments = dict(expected_execution_run_id=self.run.run_id,
            expected_snapshot_hash=self.fresh.semantic_hash, read_identity=self.read,
            read_credential_binding_hash=HASH, write_identity=self.write,
            write_credential_binding_hash=HASH, actor=LOCAL_ACTOR)
        arguments.update(overrides)
        return self.service.close_interrupted_run_for_recomparison(self.original.workspace_id, **arguments)

    def test_fresh_unique_comparison_preserves_receipts_and_unattempted_evidence(self):
        self.close()
        self.journal.close_interrupted_run_for_recomparison.assert_called_once_with(
            self.original.workspace_id, self.run, self.fresh.preflight_run_id, actor=LOCAL_ACTOR)

    def test_stale_snapshot_key_or_principal_cannot_close_the_run(self):
        for changes in ({'expected_snapshot_hash': HASH}, {'write_credential_binding_hash': 'sha256:' + '3' * 64},
                        {'write_identity': replace(self.write, principal_hash='sha256:' + '4' * 64)}):
            with self.subTest(changes=changes), self.assertRaises(WorkspaceError):
                self.close(**changes)
        self.journal.close_interrupted_run_for_recomparison.assert_not_called()

    def test_rebuilt_preparation_trace_hashes_do_not_change_source_or_business_identity(self):
        snapshot = replace(self.fresh, rows=tuple(replace(row, source_trace_id='sha256:' + str(index + 7) * 64)
                                                  for index, row in enumerate(self.fresh.rows)))
        self.service.current_preview.return_value = replace(self.preview, snapshot=snapshot)
        self.close(expected_snapshot_hash=snapshot.semantic_hash)
        self.journal.close_interrupted_run_for_recomparison.assert_called_once_with(
            self.original.workspace_id, self.run, snapshot.preflight_run_id, actor=LOCAL_ACTOR)

    def test_missing_or_different_original_receipt_cannot_close_the_run(self):
        for row in (replace(self.fresh.rows[0], disposition='CREATE', target_match_count=0, target_binding_hash=''),
                    replace(self.fresh.rows[0], target_binding_hash=target_record_binding_hash('product.category', 72))):
            with self.subTest(row=row), self.assertRaises(WorkspaceError):
                snapshot = replace(self.fresh, rows=(row, self.fresh.rows[1]))
                self.service.current_preview.return_value = replace(self.preview, snapshot=snapshot)
                self.close(expected_snapshot_hash=snapshot.semantic_hash)
        self.journal.close_interrupted_run_for_recomparison.assert_not_called()

    def test_changed_prepared_identity_cannot_close_the_run(self):
        snapshot = replace(self.fresh, rows=(self.fresh.rows[0],
                           replace(self.fresh.rows[1], business_identity=('different',))))
        self.service.current_preview.return_value = replace(self.preview, snapshot=snapshot)
        with self.assertRaisesRegex(WorkspaceError, 'same original prepared rows'):
            self.close(expected_snapshot_hash=snapshot.semantic_hash)
        self.journal.close_interrupted_run_for_recomparison.assert_not_called()
