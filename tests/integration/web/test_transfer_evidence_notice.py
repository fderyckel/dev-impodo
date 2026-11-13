"""Render recovery guidance from projected state without opening local stores."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from tests.support.paths import REPOSITORY_ROOT


class TransferEvidenceNoticeTests(unittest.TestCase):
    def setUp(self):
        self.templates = Environment(
            loader=FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
            autoescape=True,
        )

    def render(self, template, step, *, run=None):
        workspace = SimpleNamespace(
            workspace_id="workspace",
            source_mode=SimpleNamespace(value="ODOO"),
            destination_company=None,
            transfer_evidence_recovery_step=step,
        )
        with collect_duckdb_request_timings() as timings:
            html = self.templates.get_template(template).render(
                request=SimpleNamespace(
                    url=SimpleNamespace(
                        path="/workspaces/workspace/transfer-load/outcome"
                    )
                ),
                url_for=lambda _name, **values: "/static" + values.get("path", ""),
                workspace_state=workspace,
                workspace_id="workspace",
                migration_project=SimpleNamespace(display_name="Fictional migration"),
                csrf_token="csrf",
                transfer_step="outcome",
                transfer_run=run,
                reconciliation=None,
                transfer_snapshot=SimpleNamespace(semantic_hash="snapshot"),
                transfer_preflight_report=SimpleNamespace(content_hash="preflight"),
                transfer_preflight_current=True,
                transfer_dataset_rows=(),
                destination_credential_status=SimpleNamespace(available=True),
            )
        self.assertEqual(timings.connection_count, 0)
        return html

    def test_base_notice_points_to_the_required_fresh_check(self):
        for step in ("destination-matching", "transfer-review"):
            with self.subTest(step=step):
                html = self.render("base.html", step)
                self.assertIn("data-transfer-evidence-recovery", html)
                self.assertIn("Your saved source data is preserved", html)
                self.assertIn(f'href="/workspaces/workspace/{step}"', html)
                self.assertIn("inspect the saved load outcome first", html)
                self.assertNotIn("Assess and resume interrupted transfer", html)

    def test_retired_outcome_hides_resume_even_if_a_stale_current_flag_is_supplied(
        self,
    ):
        run = SimpleNamespace(
            status=SimpleNamespace(value="RUNNING"), run_id="retained-journal"
        )
        for step in ("destination-matching", "transfer-review"):
            with self.subTest(step=step):
                html = self.render("workspace_transfer_load.html", step, run=run)
                self.assertIn("Saved outcome needs review", html)
                self.assertIn("Saved journal preserved", html)
                self.assertIn("cannot be replayed or used to resume writes", html)
                self.assertNotIn(
                    'action="/workspaces/workspace/transfer-load/recover"', html
                )
                self.assertNotIn("Assess and resume interrupted transfer", html)

    def test_current_outcome_retains_the_existing_confirmed_recovery_action(self):
        run = SimpleNamespace(
            status=SimpleNamespace(value="RUNNING"), run_id="current-journal"
        )
        html = self.render("workspace_transfer_load.html", None, run=run)
        self.assertNotIn("data-transfer-evidence-recovery", html)
        self.assertIn('action="/workspaces/workspace/transfer-load/recover"', html)
        self.assertIn("Assess and resume interrupted transfer", html)
