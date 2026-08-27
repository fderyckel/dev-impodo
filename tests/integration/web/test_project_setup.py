"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from impodo.application.data_version.inspection import SourceInspectionError
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    WorkspaceStatus,
    _created_workspace_id,
)


class ProjectSetupBrowserTests(ProjectSetupBrowserTestCase):
    def test_new_project_asks_only_for_name_and_source_mode(self) -> None:
        new_page = self.client.get("/projects/new")
        self.assertIn("Data project name", new_page.text)
        self.assertIn(">Files<", new_page.text)
        self.assertIn(">Data already in Odoo<", new_page.text)
        self.assertNotIn('name="source_system"', new_page.text)
        self.assertNotIn('name="export_date"', new_page.text)
        self.assertNotIn("Data manager", new_page.text)
        self.assertNotIn("Odoo web address", new_page.text)

        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Customer migration",
                "source_mode": "FILE",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        data_project_id = self.app.state.context.migration_workspaces.get(
            workspace_id,
            actor=self.app.state.context.actor,
        ).project_id
        self.assertEqual(
            created.headers["location"],
            f"/projects/{data_project_id}",
        )
        workspace_state = self.app.state.context.queries.get(workspace_id)
        self.assertEqual(workspace_state.source_system, "Uploaded files")
        self.assertEqual(workspace_state.odoo_base_url, "")

        removed_governance = self.client.get(
            f"/workspaces/{workspace_id}/governance",
            follow_redirects=False,
        )
        self.assertEqual(removed_governance.status_code, 404)

    def test_setup_shows_each_blocker_before_the_user_can_move_forward(
        self,
    ) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Products migration",
                "source_mode": "FILE",
                "source_system_identity": "Dynamics AX 2012",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        context = self.app.state.context
        data_project_id = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        ).project_id

        files_page = self.client.get(f"/workspaces/{workspace_id}/files")
        self.assertIn("Add files to this Data version", files_page.text)
        self.assertIn("Add at least one source file.", files_page.text)
        self.assertIn('class="setup-step attention current"', files_page.text)
        self.assertNotIn('class="setup-step locked"', files_page.text)
        self.assertIn('class="sidebar-current-project-link"', files_page.text)
        self.assertIn(
            f'href="/projects/{data_project_id}"',
            files_page.text,
        )
        self.assertIn(
            'aria-label="Open data project overview: Products migration"',
            files_page.text,
        )
        self.assertNotIn("Governance", files_page.text)
        self.assertNotIn("Export date", files_page.text)
        self.assertNotIn("Odoo destination", files_page.text)

        registration_fallback = self._post(
            f"/workspaces/{workspace_id}/register",
            {"csrf_token": self.csrf, "revision": "1"},
        )
        self.assertEqual(registration_fallback.status_code, 422)
        self.assertIn("At least one source file is required", registration_fallback.text)
        self.assertNotIn("Source export", registration_fallback.text)
        self.assertNotIn("Responsible data manager", registration_fallback.text)

        blocked_target = self.client.get(
            f"/workspaces/{workspace_id}/target",
            follow_redirects=False,
        )
        self.assertEqual(blocked_target.status_code, 303)
        self.assertEqual(
            blocked_target.headers["location"],
            f"/workspaces/{workspace_id}/files",
        )

        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files={
                "source_file": (
                    "products.csv",
                    b"code,name\nP001,Example\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        ready_files_page = self.client.get(uploaded.headers["location"])
        self.assertNotIn("Complete this step to continue", ready_files_page.text)
        self.assertIn("Use these files and continue", ready_files_page.text)
        self.assertNotIn("Odoo web address", ready_files_page.text)

    def test_incomplete_file_project_can_be_left_and_resumed(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Waiting for files",
                "source_mode": "FILE",
                "source_system_identity": "Dynamics AX 2012",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        context = self.app.state.context
        data_project_id = context.migration_workspaces.get(
            workspace_id,
            actor=context.actor,
        ).project_id

        project_list = self.client.get("/projects")
        self.assertIn("Waiting for files", project_list.text)
        project_page = self.client.get(f"/projects/{data_project_id}")
        self.assertIn("Continue preparing and matching", project_page.text)
        self.assertIn(
            f'href="/workspaces/{workspace_id}/overview"',
            project_page.text,
        )

        workspace_state = self.app.state.context.queries.get(workspace_id)
        self.assertEqual(workspace_state.status, WorkspaceStatus.DRAFT)
        resumed = self.client.get(
            f"/workspaces/{workspace_id}",
            follow_redirects=False,
        )
        self.assertEqual(
            resumed.headers["location"],
            f"/workspaces/{workspace_id}/files",
        )

    def test_automatic_source_check_failure_keeps_the_retry_path(self) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Source check recovery",
                "source_mode": "FILE",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files={
                "source_file": (
                    "customers.csv",
                    b"code,name\nC001,Example\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)

        context = self.app.state.context
        original_inspect_project = context.inspections.inspect_project

        def fail_inspection(*_args, **_kwargs):
            raise SourceInspectionError("Impodo could not read the source file.")

        context.inspections.inspect_project = fail_inspection
        try:
            workspace_state = context.queries.get(workspace_id)
            registered = self._post(
                f"/workspaces/{workspace_id}/register",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )
        finally:
            context.inspections.inspect_project = original_inspect_project

        self.assertEqual(registered.status_code, 303)
        self.assertEqual(
            registered.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-files",
        )
        self.assertEqual(
            context.queries.get(workspace_id).status,
            WorkspaceStatus.REGISTERED,
        )
        recovery_page = self.client.get(registered.headers["location"])
        self.assertEqual(recovery_page.status_code, 422)
        self.assertIn("Impodo could not read the source file.", recovery_page.text)
        self.assertIn("Check source files", recovery_page.text)

    def test_incompatible_project_explains_recovery(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Historical rehearsal",
            source_system="Other",
        )
        repository = context.workspace_states.repository
        workspace_dir = repository.workspace_directory(workspace_state.workspace_id)
        database_path = workspace_dir / "workspace-engine.duckdb"
        with repository._connect(database_path) as connection:
            connection.execute("DROP TABLE schema_version")
            connection.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO schema_version VALUES (1)")

        opened = self.client.get(f"/workspaces/{workspace_state.workspace_id}")
        self.assertEqual(opened.status_code, 409)
        self.assertIn("uses a saved-data generation or version", opened.text)
        self.assertNotIn("Traceback", opened.text)
