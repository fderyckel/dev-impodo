"""Verify static, contextual access to every current user guide."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.web.presenters.user_help import (
    DECISION_HELP_BY_ID,
    PAGE_HELP_ID_BY_TEMPLATE,
    USER_HELP_BY_ID,
    USER_HELP_TARGETS,
    UserHelpScope,
    installed_documentation_ref,
)
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.paths import REPOSITORY_ROOT


class UserHelpRegistryTests(unittest.TestCase):
    """Keep the packaged help registry complete and value-free."""

    def test_registry_covers_every_current_user_guide(self) -> None:
        user_root = REPOSITORY_ROOT / "docs" / "user"
        actual_paths = {
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in user_root.rglob("*.md")
        }
        registered_paths = {
            target.document_path for target in USER_HELP_TARGETS
        }

        self.assertEqual(registered_paths, actual_paths)
        self.assertEqual(len(USER_HELP_BY_ID), len(USER_HELP_TARGETS))
        self.assertTrue(
            all(target.scope in UserHelpScope for target in USER_HELP_TARGETS)
        )

    def test_every_workflow_template_has_page_help(self) -> None:
        import yaml

        workflow = yaml.safe_load(
            (REPOSITORY_ROOT / "docs" / "workflow.yml").read_text(
                encoding="utf-8"
            )
        )
        prefix = "src/impodo/web/templates/"

        for stage in workflow["stages"]:
            with self.subTest(stage=stage["id"]):
                help_id = stage["help_id"]
                self.assertEqual(
                    USER_HELP_BY_ID[help_id].document_path,
                    stage["user_doc"],
                )
                for template in stage["templates"]:
                    template_name = template.removeprefix(prefix)
                    self.assertIn(template_name, PAGE_HELP_ID_BY_TEMPLATE)

    def test_release_builds_use_the_matching_tag(self) -> None:
        installed = MagicMock(version="1.4.2")
        installed.read_text.return_value = None
        with patch(
            "impodo.web.presenters.user_help.distribution",
            return_value=installed,
        ):
            self.assertEqual(installed_documentation_ref(), "v1.4.2")
            installed.version = "0.0.0"
            self.assertEqual(installed_documentation_ref(), "main")

    def test_editable_checkouts_use_current_guides_after_a_release(self) -> None:
        installed = MagicMock(version="26.10.4")
        installed.read_text.return_value = '{"dir_info": {"editable": true}}'
        with patch(
            "impodo.web.presenters.user_help.distribution",
            return_value=installed,
        ):
            self.assertEqual(installed_documentation_ref(), "main")

    def test_documentation_gate_rejects_a_removed_heading_or_placement(self) -> None:
        from scripts.documentation_quality import validate_repository

        targets = dict(DECISION_HELP_BY_ID)
        targets["rows-to-use"] = replace(
            targets["rows-to-use"],
            fragment="removed-help-section",
            template_names=(),
        )
        with patch("scripts.documentation_quality.DECISION_HELP_BY_ID", targets):
            messages = tuple(issue.message for issue in validate_repository(REPOSITORY_ROOT))

        self.assertTrue(any("removed-help-section" in message for message in messages))
        self.assertTrue(any("registered template placements" in message for message in messages))


class UserHelpBrowserTests(unittest.TestCase):
    """Prove help rendering does not open migration or Odoo state."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="impodo-user-help-"
        )
        self.root = Path(self.temporary_directory.name)
        self.app = create_local_app(
            self.root,
            launch_token="help-launch",
            session_secret="help-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary_directory.cleanup()

    def _launch(self) -> None:
        response = self.client.get(
            "/launch?token=help-launch",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_help_page_requires_the_browser_session(self) -> None:
        response = self.client.get("/help")

        self.assertEqual(response.status_code, 401)

    def test_help_page_lists_every_guide_without_database_or_odoo_work(self) -> None:
        self._launch()
        context = self.app.state.context
        migration_projects = MagicMock(wraps=context.migration_projects)
        boundary_names = (
            "connection_tester",
            "read_identity_probe",
            "write_identity_probe",
            "schema_reader",
            "model_catalog_reader",
            "readiness_reader",
            "source_capture_factory",
            "write_executor_factory",
            "readback_reader_factory",
        )
        boundary_patches = [
            patch.object(
                context,
                name,
                MagicMock(side_effect=AssertionError(f"{name} must not run")),
            )
            for name in boundary_names
        ]

        with (
            patch.object(context, "migration_projects", migration_projects),
            collect_duckdb_request_timings() as timings,
        ):
            for boundary_patch in boundary_patches:
                boundary_patch.start()
            try:
                response = self.client.get("/help")
            finally:
                for boundary_patch in reversed(boundary_patches):
                    boundary_patch.stop()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(timings.connection_count, 0)
        self.assertEqual(timings.schema_check_count, 0)
        migration_projects.list.assert_not_called()
        for target in USER_HELP_TARGETS:
            with self.subTest(help_id=target.help_id):
                self.assertIn(
                    f'data-user-help-id="{target.help_id}"',
                    response.text,
                )
                self.assertIn(target.url, response.text)
        self.assertIn('href="/help"', response.text)
        self.assertIn('aria-current="page"', response.text)

    def test_project_list_links_directly_to_the_setup_guide(self) -> None:
        self._launch()
        context = self.app.state.context
        project_queries = MagicMock(wraps=context.project_queries)

        with patch.object(context, "project_queries", project_queries):
            response = self.client.get("/projects")

        self.assertEqual(response.status_code, 200)
        project_queries.list_visible_project_summaries.assert_called_once()
        target = USER_HELP_BY_ID["getting-started"]
        self.assertIn('data-page-help-slot', response.text)
        self.assertIn('data-user-help-id="getting-started"', response.text)
        self.assertIn(f'href="{target.url}"', response.text)
        self.assertIn('target="_blank"', response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)
        self.assertIn(f'aria-label="{target.accessible_name}"', response.text)

    def test_page_help_script_only_places_the_static_link(self) -> None:
        self._launch()

        response = self.client.get("/static/user-help.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn('document.querySelector("[data-page-help-slot]")', response.text)
        self.assertIn('".page-heading h1, .projects-hero h1"', response.text)
        self.assertIn("headingRow.append(pageTitle, helpSlot)", response.text)
        self.assertNotIn("fetch(", response.text)

    def test_help_styles_keep_a_touch_target_and_one_narrow_column(self) -> None:
        response = self.client.get("/static/user-help.css")

        self.assertEqual(response.status_code, 200)
        self.assertIn("width: 2.75rem", response.text)
        self.assertIn("height: 2.75rem", response.text)
        self.assertIn("@media (max-width: 860px)", response.text)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", response.text)
        self.assertIn("overflow-wrap: anywhere", response.text)


class DecisionHelpBrowserTests(ProjectSetupBrowserTestCase):
    """Keep anchored guidance beside real decisions without changing saved work."""

    def test_schema_and_mapping_links_keep_the_saved_choices(self) -> None:
        workspace_id, _dataset, _key = self._mapping_ready_workspace(scalar_field_count=1)
        context = self.app.state.context
        draft = context.queries.get_mapping_working_draft(workspace_id)
        calls = (tuple(self.schema_calls), tuple(self.readiness_calls))

        for area, help_ids in (
            ("schema", ("odoo-identity",)),
            ("mapping", ("source-identity", "target-identity", "rows-to-use", "field-values", "relationships")),
        ):
            with self.subTest(area=area):
                response = self.client.get(f"/workspaces/{workspace_id}/{area}")
                self.assertEqual(response.status_code, 200)
                for help_id in help_ids:
                    target = DECISION_HELP_BY_ID[help_id]
                    self.assertIn(f'data-user-help-id="{help_id}"', response.text)
                    self.assertIn(f'href="{target.url}"', response.text)
                    self.assertIn(f'aria-label="{target.accessible_name}"', response.text)

        self.assertEqual(context.queries.get_mapping_working_draft(workspace_id), draft)
        self.assertEqual((tuple(self.schema_calls), tuple(self.readiness_calls)), calls)

    def test_recovery_guidance_does_not_assess_or_resume_the_load(self) -> None:
        from tests.integration.web.test_load_recovery import LoadRecoveryBrowserTests

        fixture = LoadRecoveryBrowserTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with fixture.recovery_context():
            response = fixture.client.get(
                f"/workspaces/{fixture.workspace.workspace_id}/load/outcome"
            )

        target = DECISION_HELP_BY_ID["load-recovery"]
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'href="{target.url}"', response.text)
        self.assertIn('target="_blank"', response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)
        self.assertEqual(fixture.events, [])


if __name__ == "__main__":
    unittest.main()
