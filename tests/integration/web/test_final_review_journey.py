"""Final review locations preserve saved checks and never run business actions."""

from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.execution.business_review import BusinessIntentReview
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.presenters.final_review_journey import final_review_journey
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class _ReviewSections(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.sections = {}
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "section" and values.get("id"):
            self.sections[values["id"]] = values.get("class", "").split()


class FinalReviewPresentationTests(unittest.TestCase):
    def test_direct_steps_before_preparation_point_to_the_existing_preparation_action(self):
        templates = Path(__file__).resolve().parents[3] / "src/impodo/web/templates"
        environment = Environment(loader=FileSystemLoader(templates))
        for name in ("_compare.html", "_results.html"):
            with self.subTest(page=name):
                markup = environment.get_template("final_review/" + name).render(
                    workspace_id="workspace", readiness=None, normalization=None,
                    workspace_state=SimpleNamespace(
                        odoo_database="Training", odoo_connection_mode=OdooConnectionMode.REMOTE,
                    ),
                )
                self.assertIn('href="/workspaces/workspace/summary?step=prepared"', markup)
                self.assertNotIn('href="/workspaces/workspace/normalization"', markup)
                self.assertNotIn('action="/workspaces/workspace/summary/compare"', markup)

    def test_accepted_omissions_do_not_hide_later_expectation_deviation(self):
        templates = Path(__file__).resolve().parents[3] / "src/impodo/web/templates"
        environment = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(templates),
        ]))
        markup = environment.get_template("workspace_summary.html").render(
            migration_project=SimpleNamespace(display_name="Review example"),
            migration_context=SimpleNamespace(project_id="project", migration_run_id="run"),
            workspace_navigation=SimpleNamespace(journey="AUTHORING"),
            workspace_state=SimpleNamespace(
                source_mode=SourceMode.FILE, odoo_connection_mode=OdooConnectionMode.REMOTE,
                revision=3,
            ),
            workspace_id="workspace", final_review_journey={"step": "results", "view": "attention"},
            readiness=SimpleNamespace(
                run_id="comparison", status="READY", blocked_count=0, needs_review_count=0,
                create_count=5, update_count=0, unchanged_count=0, attention_count=0,
            ),
            load_preview=SimpleNamespace(
                can_load=False, can_complete_without_load=False, current_run=None,
                scope_error="Review business expectations against the proposed changes",
            ),
            business_review=BusinessIntentReview(
                state="DEVIATION", snapshot_hash="snapshot", assessment_hash="assessment",
                intent_hash="intent", intent_version=2, model_count=0, deviation_count=1,
                message="The remaining records are below the expected minimum.", models=(),
            ),
            deferred_scope=SimpleNamespace(
                decision=object(), preview=SimpleNamespace(newly_set_aside_count=2, remaining_write_count=3),
            ),
            readiness_rows=(), readiness_row_total=0, readiness_row_page=1,
            readiness_row_start=0, readiness_row_end=0, readiness_row_page_count=1,
            status_filter="attention", csrf_token="test-token", business_run_id="",
        )
        self.assertIn("Expectations need review", markup)
        self.assertNotIn('status ready">Ready with records set aside', markup)
        self.assertIn("review", _ReviewSections(markup).sections["compare-with-odoo"])
        self.assertIn("Review expected versus found", markup)
        self.assertIn("step=results&amp;view=changes#expected-versus-found", markup)
        self.assertIn("Original comparison", markup)
        self.assertNotIn("Continue to Load into Odoo", markup)


class FinalReviewLocationTests(unittest.TestCase):
    def test_older_filters_open_their_owning_panel(self):
        for query, step, view in (
            ({"quality_status": "quarantined"}, "prepared", "changes"),
            ({"status": "blocked"}, "results", "attention"),
            ({"issue": "TARGET_REFERENCE_UNRESOLVED"}, "results", "attention"),
            ({"status": "update"}, "results", "changes"),
            ({"step": "results", "view": "outside"}, "results", "outside"),
            ({"step": "compare", "status": "blocked"}, "compare", "attention"),
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    final_review_journey(query, comparison_available=True, preparation_approved=True),
                    {"step": step, "view": view},
                )

    def test_saved_state_selects_the_next_existing_responsibility(self):
        for available, approved, recovery, attention, step, view in (
            (False, False, False, False, "prepared", "changes"),
            (False, True, False, False, "compare", "changes"),
            (True, True, False, False, "results", "changes"),
            (True, True, False, True, "results", "attention"),
            (True, True, True, False, "compare", "changes"),
        ):
            with self.subTest(step=step, view=view, recovery=recovery):
                self.assertEqual(
                    final_review_journey(
                        {}, comparison_available=available, preparation_approved=approved,
                        comparison_recovery=recovery, attention_required=attention,
                    ),
                    {"step": step, "view": view},
                )

    def test_invalid_locations_fall_back_to_the_saved_state(self):
        self.assertEqual(
            final_review_journey(
                {"step": "approve", "view": "all"}, comparison_available=False,
                preparation_approved=True,
            ),
            {"step": "compare", "view": "changes"},
        )


class FinalReviewJourneyRouteTests(HierarchyWorkflowFixture):
    def test_odoo19_local(self):
        self.exercise(19, "LOCAL")

    def test_odoo19_remote(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_local(self):
        self.exercise(20, "LOCAL")

    def test_odoo20_remote(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        prefix = f"/workspaces/{workspace_id}/summary"
        target = HierarchyOdoo(self.context.queries.get_odoo_schema_catalog(workspace_id))
        before_comparison = self.client.get(prefix + "?step=results")
        self.assertIn("There is no current saved Odoo comparison", before_comparison.text)
        report = run_local_operation_sync(
            self.context.preflight.compare, workspace_id, reader=target.compare, actor=self.actor,
        )
        requests = len(target.requests)
        for step, view, title in (
            ("prepared", "changes", "Prepared data"),
            ("compare", "changes", "Compare with Odoo"),
            ("results", "attention", "Review results"),
            ("results", "changes", "Review results"),
            ("results", "outside", "Review results"),
        ):
            with self.subTest(step=step, view=view):
                with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
                    response = self.client.get(prefix + f"?step={step}&view={view}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(f"<h1>{title}</h1>", response.text)
                self.assertIn(f'data-step="{step}" data-view="{view}"', response.text)
                navigation = response.context["workspace_navigation"]
                self.assertEqual(navigation.viewed_page_label, title)
                if step != "prepared":
                    self.assertNotIn('aria-label="Data quality overview"', response.text)
                if step == "compare":
                    self.assertNotIn('id="readiness-rows"', response.text)
                    self.assertNotIn('id="quality-rows"', response.text)
                self.assertLessEqual(timings.connection_count, 4)
                self.assertLessEqual(timings.schema_check_count, 3)
                self.assertEqual(
                    sum("duckdb_columns()" in statement for statement in statements),
                    timings.schema_check_count,
                )
                self.assertEqual(timings.lock_retry_count, 0)
        with (
            patch.object(self.context.queries, "get_quality_review_page") as preparation_rows,
            patch.object(self.context.preflight, "readiness_rows") as comparison_rows,
        ):
            self.client.get(prefix + "?step=compare")
        preparation_rows.assert_not_called()
        comparison_rows.assert_not_called()
        outside = self.client.get(prefix + "?step=results&view=outside")
        self.assertIn("Outside during preparation", outside.text)
        self.assertIn("Omitted by your Final review decision", outside.text)
        self.assertIn("view=outside#records-outside-load", outside.text)
        self.assertEqual(self.context.preflight.current_report(workspace_id), report)
        self.assertEqual(len(target.requests), requests)
        self.assertEqual(target.writes, [])
