"""Render shared headers from job-only facts without opening workspace stores."""

from types import SimpleNamespace
from unittest import TestCase

from jinja2 import Environment, FileSystemLoader

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.workspace.workbench import SourceMode
from tests.support.database_probe import count_duckdb_statements
from tests.support.paths import REPOSITORY_ROOT


class BackgroundPageLayoutTests(TestCase):
    def render(self, workspace):
        templates = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )
        with (
            collect_duckdb_request_timings() as timings,
            count_duckdb_statements() as sql,
        ):
            html = templates.get_template("base.html").render(
                request=SimpleNamespace(
                    url=SimpleNamespace(path="/workspaces/workspace/preparation/job")
                ),
                url_for=lambda _name, **values: "/static" + values.get("path", ""),
                workspace_state=workspace,
                migration_project=SimpleNamespace(
                    project_id="project", display_name="Example"
                ),
                csrf_token="csrf",
            )
        self.assertEqual(timings.connection_count, 0)
        self.assertEqual(timings.schema_check_count, 0)
        self.assertEqual(len(sql), 0)
        return html

    def test_job_only_workspace_does_not_require_source_or_destination_metadata(self):
        workspace = SimpleNamespace(
            workspace_id="workspace", name="Example", registered_at=True
        )
        html = self.render(workspace)
        self.assertIn("</html>", html)
        self.assertNotIn("data-database-binding-review", html)

    def test_full_odoo_workspace_keeps_destination_review_warning(self):
        for version in ("19.0", "20.0"):
            with self.subTest(version=version):
                workspace = SimpleNamespace(
                    workspace_id="workspace",
                    name="Example",
                    registered_at=True,
                    source_mode=SourceMode.ODOO,
                    odoo_version=version,
                    destination_configured=True,
                    destination_database_binding_current=False,
                )
                html = self.render(workspace)
                self.assertIn("Review the destination database before loading", html)
                self.assertIn(
                    "/projects/project/odoo-database-bindings?workspace_id=workspace",
                    html,
                )
                workspace.destination_database_binding_current = True
                self.assertNotIn("data-database-binding-review", self.render(workspace))

    def test_file_workspace_does_not_show_destination_database_warning(self):
        workspace = SimpleNamespace(
            workspace_id="workspace",
            name="Example",
            registered_at=True,
            source_mode=SourceMode.FILE,
        )
        self.assertNotIn("data-database-binding-review", self.render(workspace))
