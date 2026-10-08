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
        for raw in ("21.0", "19.garbage", "unknown", "saas~19.4"):
            for operation in (OdooOperation.WRITE, OdooOperation.RECOVER):
                with self.subTest(raw=raw, operation=operation):
                    self.assertEqual(
                        _execution_snapshot_error(
                            workspace,
                            SimpleNamespace(target_odoo_version=raw),
                            operation=operation,
                        ),
                        (
                            "This Odoo version is not enabled for the "
                            "schema-bound load path"
                        ),
                    )

    def test_load_shape_accepts_supported_odoo20(self):
        workspace = SimpleNamespace(odoo_connection_mode=OdooConnectionMode.REMOTE)
        snapshot = SimpleNamespace(
            target_odoo_version="20.0",
            relationship_plan=SimpleNamespace(
                contract_version=0,
                components=(),
                blockers=(),
                completions=(),
                root_hash="",
            ),
            rows=(),
            datasets=(),
        )
        self.assertEqual(
            _execution_snapshot_error(workspace, snapshot),
            (
                "The reviewed relationship schedule changed. Compare with Odoo "
                "again before loading."
            ),
        )

    def test_resume_blocks_before_journal_or_writer_access(self):
        for raw in ("19.garbage", "unknown"):
            with self.subTest(raw=raw):
                workspace = SimpleNamespace(
                    source_mode=SourceMode.FILE,
                    odoo_connection_mode=OdooConnectionMode.REMOTE,
                )
                workspaces = Mock()
                workspaces.get.return_value = workspace
                preflight = Mock()
                preflight.current_execution_snapshot.return_value = SimpleNamespace(
                    target_odoo_version=raw
                )
                journal, writer = Mock(), Mock()
                service = ExecutionService(workspaces, preflight, journal, Mock())
                with self.assertRaisesRegex(
                    WorkspaceError,
                    "not enabled for the schema-bound load path",
                ):
                    service.resume(
                        "workspace",
                        expected_execution_run_id="run",
                        recovery=Mock(),
                        executor=writer,
                        actor=Mock(),
                    )
                self.assertEqual(journal.mock_calls, [])
                self.assertEqual(writer.mock_calls, [])

    def test_resume_reaches_journal_for_supported_odoo20(self):
        workspace = SimpleNamespace(
            source_mode=SourceMode.FILE,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
        )
        workspaces = Mock()
        workspaces.get.return_value = workspace
        preflight = Mock()
        preflight.current_execution_snapshot.return_value = SimpleNamespace(
            target_odoo_version="20.0",
            semantic_hash="sha256:" + "a" * 64,
        )
        journal, writer = Mock(), Mock()
        journal.get_run.return_value = None
        journal.get_current_run.return_value = None
        service = ExecutionService(workspaces, preflight, journal, Mock())
        with self.assertRaisesRegex(
            WorkspaceError,
            "interrupted load or preview is no longer current",
        ):
            service.resume(
                "workspace",
                expected_execution_run_id="run",
                recovery=Mock(),
                executor=writer,
                actor=Mock(),
            )
        journal.get_run.assert_called_once_with("workspace", "run")
        self.assertEqual(writer.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
