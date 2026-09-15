"""Reject disabled or unverified versions before recovery can mutate a journal."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from impodo.application.workspace.execution.service import ExecutionService, _execution_snapshot_error
from impodo.domain.odoo.compatibility import OdooOperation
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode


class ExecutionOdooVersionTests(unittest.TestCase):
    def test_load_shape_blocks_disabled_or_unrecognized_versions(self):
        workspace = SimpleNamespace(odoo_connection_mode=OdooConnectionMode.REMOTE)
        for raw in ("20.0", "21.0", "19.garbage", "unknown", "saas~19.4"):
            for operation in (OdooOperation.WRITE, OdooOperation.RECOVER):
                with self.subTest(raw=raw, operation=operation):
                    self.assertEqual(
                        _execution_snapshot_error(
                            workspace, SimpleNamespace(target_odoo_version=raw), operation=operation,
                        ),
                        "The schema-bound load path requires Odoo 19",
                    )

    def test_resume_blocks_before_journal_or_writer_access(self):
        for raw in ("20.0", "19.garbage", "unknown"):
            with self.subTest(raw=raw):
                workspace = SimpleNamespace(
                    source_mode=SourceMode.FILE, odoo_connection_mode=OdooConnectionMode.REMOTE,
                )
                workspaces = Mock()
                workspaces.get.return_value = workspace
                preflight = Mock()
                preflight.current_execution_snapshot.return_value = SimpleNamespace(target_odoo_version=raw)
                journal, writer = Mock(), Mock()
                service = ExecutionService(workspaces, preflight, journal, Mock())
                with self.assertRaisesRegex(WorkspaceError, "requires Odoo 19"):
                    service.resume(
                        "workspace", expected_execution_run_id="run", recovery=Mock(),
                        executor=writer, actor=Mock(),
                    )
                self.assertEqual(journal.mock_calls, [])
                self.assertEqual(writer.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
