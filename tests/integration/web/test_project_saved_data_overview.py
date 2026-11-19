"""Exercise contextual saved-data entry points and offline project summaries."""

from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo_saved_data import SavedOdooDataDefinition
from impodo.domain.serialization import content_hash
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class ProjectSavedDataOverviewTests(ProjectSetupBrowserTestCase):
    def _project(self, mode):
        response = self._post("/projects/new", {
            "csrf_token": self.csrf,
            "display_name": f"Saved data overview {mode}",
            "source_mode": mode,
        })
        self.assertEqual(response.status_code, 303, response.text)
        return response.headers["location"]

    def test_secondary_actions_follow_source_and_open_package_import(self):
        for mode in ("FILE", "ODOO"):
            with self.subTest(mode=mode):
                path = self._project(mode)
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertNotIn("Keep clean data ready", page.text)
                self.assertNotIn('id="project-saved-odoo-data-title"', page.text)
                self.assertEqual("Set up saved Odoo data" in page.text, mode == "ODOO")
                import_url = f"{path}/saved-odoo-data?task=import#import-saved-data"
                self.assertIn(f'href="{import_url}"', page.text)
                entry = self.client.get(import_url)
                self.assertEqual(entry.status_code, 200, entry.text)
                self.assertIn('id="import-saved-data" open', entry.text)

    def test_existing_sets_on_file_project_use_one_bounded_summary_read(self):
        path = self._project("FILE")
        project_id = path.split("/")[-1]
        context = self.app.state.context
        workspace, = context.migration_workspaces.list_for_project(
            project_id, actor=resolve_local_actor(context)
        )
        repository = context.saved_odoo_data.repository
        now = datetime.now(UTC)
        definition = SavedOdooDataDefinition(
            saved_data_id=str(uuid4()), project_id=project_id, label="Pilot configuration",
            revision=1, source_workspace_id=workspace.workspace_id,
            source_target_hash=content_hash("database"),
            source_company_hash=content_hash("company"), odoo_major=20,
            config_json="{}", latest_version=None, created_at=now, updated_at=now,
        )
        repository.create_definition(definition, source_binding_hash=None)
        solo_page = self.client.get(path)
        self.assertEqual(solo_page.status_code, 200, solo_page.text)
        self.assertIn('id="project-current-title">Pilot configuration', solo_page.text)
        self.assertIn("No data captured yet", solo_page.text)
        self.assertNotIn('id="project-saved-odoo-data-title"', solo_page.text)
        for index in range(4):
            repository.create_definition(
                replace(definition, saved_data_id=str(uuid4()), label=f"Pilot set {index}"),
                source_binding_hash=None,
            )
        with collect_duckdb_request_timings() as timing, count_duckdb_statements() as sql:
            items = repository.list_set_summaries(project_id, limit=4)
        self.assertEqual(len(items), 4)
        self.assertEqual(timing.connection_count, 1)
        self.assertEqual(timing.schema_check_count, 1)
        self.assertLessEqual(len(sql), 5)  # Registry validation and one joined query.
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.text.count("Configuration saved · No data captured yet"), 3)
        self.assertIn("View saved sets", page.text)
        self.assertNotIn("Set up saved Odoo data", page.text)
        manager = context.saved_odoo_jobs
        with patch.object(manager._runner, "submit"):
            job = manager.enqueue(
                project_id, items[-1].saved_data_id, kind="UPDATE", work=lambda _: "/result"
            )
        progress_url = f"{path}/saved-odoo-data?selected={job.saved_data_id}&job={job.job_id}"
        self.assertIn(progress_url, unescape(self.client.get(path).text))
        progress_page = self.client.get(progress_url)
        self.assertEqual(progress_page.status_code, 200, progress_page.text)
        self.assertIn(f'href="{progress_url}">Refresh progress', unescape(progress_page.text))
        unrelated = self.client.get(self._project("FILE"))
        self.assertNotIn("Pilot set", unrelated.text)
        self.assertNotIn(job.job_id, unrelated.text)
