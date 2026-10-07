"""Verify authorized page selection and narrow, correctly scoped recovery counts."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.migration_run_planning_repository import MigrationRunPlanningRepository
from impodo.application.run.setup_service import RunSetupService
from impodo.domain.run.test_setup import TestRunSetupState
from impodo.domain.shared.access import Capability


class RunSetupPageTests(unittest.TestCase):
    def service(self, binding):
        test_repository = Mock()
        test_repository.for_workspace.return_value = binding
        production_repository = Mock()
        production_repository.for_workspace.return_value = None
        planning = Mock()
        planning.repository.count_blocking_run_issues.return_value = 3
        service = RunSetupService(
            test_runs=SimpleNamespace(test_runs=test_repository, run_planning=planning),
            production_runs=SimpleNamespace(production_runs=production_repository),
            runs=Mock(), recipes=Mock(), authorization=Mock(),
        )
        return service, test_repository, production_repository, planning

    def test_absent_setup_is_retained_without_repeated_ownership_reads(self):
        service, test_repository, production_repository, planning = self.service(None)
        page = service.page_for_workspace("workspace", actor="actor")
        self.assertIsNone(service.odoo_check_requirements_for_page(page, actor="actor"))
        self.assertEqual(service.required_default_count_for_page(page, actor="actor"), 0)
        test_repository.for_workspace.assert_called_once_with("workspace")
        production_repository.for_workspace.assert_called_once_with("workspace")
        planning.repository.count_blocking_run_issues.assert_not_called()

    def test_active_test_setup_uses_a_count_without_loading_application_evidence(self):
        binding = SimpleNamespace(project_id="project", migration_run_id="run", setup_workspace_id="workspace",
                                  selected_revisions=(), state=TestRunSetupState.ACTIVE)
        service, test_repository, production_repository, planning = self.service(binding)
        service.odoo.for_selection = Mock(return_value="requirements")
        page = service.page_for_workspace("workspace", actor="actor")
        self.assertEqual(service.odoo_check_requirements_for_page(page, actor="actor"), "requirements")
        self.assertEqual(service.required_default_count_for_page(page, actor="actor"), 3)
        test_repository.for_workspace.assert_called_once()
        production_repository.for_workspace.assert_not_called()
        service.odoo.for_selection.assert_called_once_with(page.selection, actor="actor")
        planning.repository.count_blocking_run_issues.assert_called_once_with("run", code="RECIPE_TARGET_NEW_REQUIRED_FIELD")
        self.assertIn(call("actor", Capability.PROJECT_VIEW, project_id="project"), service.authorization.require.call_args_list)

    def test_active_child_workspace_does_not_show_setup_recovery(self):
        binding = SimpleNamespace(project_id="project", migration_run_id="run", setup_workspace_id="shared",
                                  selected_revisions=(), state=TestRunSetupState.ACTIVE)
        service, _, _, planning = self.service(binding)
        page = service.page_for_workspace("child", actor="actor")
        self.assertFalse(page.selection.active_test_setup)
        self.assertEqual(service.required_default_count_for_page(page, actor="actor"), 0)
        planning.repository.count_blocking_run_issues.assert_not_called()

    def test_setup_page_preserves_access_rejection(self):
        binding = SimpleNamespace(project_id="project", migration_run_id="run", setup_workspace_id="workspace",
                                  selected_revisions=(), state=TestRunSetupState.SETUP)
        service, _, _, planning = self.service(binding)
        service.authorization.require.side_effect = PermissionError("Fictional denied access")
        with self.assertRaises(PermissionError):
            service.page_for_workspace("workspace", actor="actor")
        planning.repository.count_blocking_run_issues.assert_not_called()

    def test_production_setup_retains_qualified_plan_and_has_no_test_recovery(self):
        service, _, production_repository, planning = self.service(None)
        binding = SimpleNamespace(project_id="project", migration_run_id="run", setup_workspace_id="workspace",
                                  cutover_plan_id="plan", cutover_plan_revision=2, plan_content_hash="sha256:qualified")
        production_repository.for_workspace.return_value = binding
        service.production_runs.cutover_plans = Mock()
        service.production_runs.cutover_plans.get_revision.return_value = SimpleNamespace(
            content_hash="sha256:qualified", selected_revisions=(),
        )
        page = service.page_for_workspace("workspace", actor="actor")
        self.assertEqual(page.selection.migration_run_id, "run")
        self.assertFalse(page.selection.active_test_setup)
        self.assertEqual(service.required_default_count_for_page(page, actor="actor"), 0)
        planning.repository.count_blocking_run_issues.assert_not_called()
        service.production_runs.cutover_plans.get_revision.return_value.content_hash = "sha256:changed"
        with self.assertRaisesRegex(Exception, "qualified plan"):
            service.page_for_workspace("workspace", actor="actor")


class BlockingRunIssueCountTests(unittest.TestCase):
    def test_count_matches_issue_level_code_and_run_without_reading_full_rows(self):
        connection = duckdb.connect(config={"threads":"1"})
        try:
            connection.execute("CREATE TABLE recipe_application (application_id VARCHAR, migration_run_id VARCHAR)")
            connection.execute("CREATE TABLE recipe_application_issue (application_id VARCHAR, code VARCHAR, level VARCHAR)")
            run_id, other_run = str(uuid4()), str(uuid4())
            connection.executemany("INSERT INTO recipe_application VALUES (?,?)", [("first",run_id),("second",run_id),("other",other_run)])
            code = "RECIPE_TARGET_NEW_REQUIRED_FIELD"
            connection.executemany("INSERT INTO recipe_application_issue VALUES (?,?,?)", [
                ("first",code,"BLOCKER"),("first",code,"BLOCKER"),("second",code,"BLOCKER"),
                ("second",code,"REVIEW"),("first","OTHER","BLOCKER"),("other",code,"BLOCKER"),
            ])
            @contextmanager
            def connect(_path):
                yield connection
            repository = MigrationRunPlanningRepository.__new__(MigrationRunPlanningRepository)
            repository.registry_path = "unused"
            repository.database = SimpleNamespace(connect=connect)
            with (patch.object(repository, "list_applications", side_effect=AssertionError("Full applications loaded")),
                  patch.object(repository, "list_run_issues", side_effect=AssertionError("Full issues loaded"))):
                self.assertEqual(repository.count_blocking_run_issues(run_id, code=code), 3)
                self.assertEqual(repository.count_blocking_run_issues(other_run, code=code), 1)
                self.assertEqual(repository.count_blocking_run_issues(str(uuid4()), code=code), 0)
        finally:
            connection.close()
