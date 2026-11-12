"""Keep review locations separate from saved comparison and approval evidence."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.preflight_jobs import (
    PreflightJob,
    PreflightJobStatus,
    PreflightPhase,
)
from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.application.workspace.navigation import WorkspaceNavigationFacts
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)
from impodo.web.presenters.navigation import (
    _recipe_application_navigation,
    build_preflight_workspace_navigation,
    build_workspace_navigation,
)
from tests.integration.web.test_transfer_order_navigation import (
    _facts as transfer_facts,
)
from tests.integration.web.test_transfer_order_navigation import (
    _stage_five_state,
)


class FinalReviewNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = WorkspaceState(
            workspace_id="workspace:review",
            name="Products",
            source_system="CSV",
            source_mode=SourceMode.FILE,
            status=WorkspaceStatus.REGISTERED,
        )
        self.facts = WorkspaceNavigationFacts(
            workspace_id=self.workspace.workspace_id,
            source_selection_hash="source",
            schema_present=True,
            governance_present=True,
            mapping_complete=True,
            staging_run_id="prepared",
            quality_run_id="quality",
            quality_staging_run_id="prepared",
            normalization_run_id="normalization",
            normalization_staging_run_id="prepared",
            normalization_quality_run_id="quality",
            normalization_status="FROZEN",
        )

    def test_opening_each_location_does_not_complete_an_unchecked_comparison(self):
        for step, label in (
            ("prepared", "Prepared data"),
            ("compare", "Compare with Odoo"),
            ("results", "Review results"),
        ):
            with self.subTest(step=step):
                navigation = build_workspace_navigation(
                    self.facts,
                    self.workspace,
                    "workspace_summary.html",
                    review_step=step,
                )
                stages = {stage.stage_id: stage for stage in navigation.stages}
                pages = stages["review"].pages
                self.assertEqual(navigation.viewed_page_label, label)
                self.assertEqual(
                    [page.label for page in pages],
                    [
                        "Prepared data",
                        "Compare with Odoo",
                        "Review results",
                    ],
                )
                self.assertEqual(
                    [page.label for page in pages if page.current], [label]
                )
                self.assertEqual(pages[0].status, "complete")
                self.assertEqual(pages[1].status, "available")
                self.assertEqual(pages[2].status_label, "Not yet compared")
                self.assertEqual(stages["review"].status, "current")
                self.assertIsNone(stages["load"].href)
                self.assertEqual(self.facts.preflight_status, "")

    def test_a_saved_blocked_comparison_has_completed_comparison_and_attention_results(
        self,
    ):
        navigation = build_workspace_navigation(
            replace(self.facts, preflight_status="BLOCKED"),
            self.workspace,
            "workspace_summary.html",
            review_step="results",
        )
        stages = {stage.stage_id: stage for stage in navigation.stages}
        self.assertEqual(stages["review"].pages[1].status, "complete")
        self.assertEqual(stages["review"].pages[2].status, "attention")
        self.assertEqual(stages["review"].status, "attention")
        self.assertIsNone(stages["load"].href)

    def test_ready_evidence_keeps_the_existing_stage_six_handoff(self):
        navigation = build_workspace_navigation(
            replace(self.facts, preflight_status="READY"),
            self.workspace,
            "workspace_summary.html",
            review_step="results",
        )
        stages = {stage.stage_id: stage for stage in navigation.stages}
        self.assertTrue(
            all(page.status == "complete" for page in stages["review"].pages)
        )
        self.assertEqual(stages["review"].status, "complete")
        self.assertIsNotNone(stages["load"].href)

    def test_details_and_progress_keep_their_owning_review_location(self):
        for template, label in (
            ("workspace_quality_cause.html", "Prepared data"),
            ("workspace_preflight_progress.html", "Compare with Odoo"),
            ("workspace_business_expectations.html", "Review results"),
            ("workspace_business_expectation_detail.html", "Review results"),
            ("workspace_deferred_scope.html", "Review results"),
        ):
            with self.subTest(template=template):
                navigation = build_workspace_navigation(
                    self.facts,
                    self.workspace,
                    template,
                )
                self.assertEqual(navigation.viewed_stage_id, "review")
                self.assertEqual(navigation.viewed_page_label, label)
                self.assertEqual(
                    [
                        page.label
                        for page in navigation.viewed_stage.pages
                        if page.current
                    ],
                    [label],
                )
        invalid = build_workspace_navigation(
            self.facts,
            self.workspace,
            "workspace_summary.html",
            review_step="load",
        )
        self.assertEqual(invalid.viewed_page_label, "Prepared data")

    def test_recipe_review_keeps_substeps_without_adding_run_stages(self):
        authoring = build_workspace_navigation(
            self.facts,
            self.workspace,
            "workspace_summary.html",
            review_step="results",
        )
        recipe = _recipe_application_navigation(
            authoring,
            project_id="project",
            migration_run_id="run",
        )
        self.assertEqual(
            [stage.label for stage in recipe.stages],
            [
                "Fresh data",
                "Check Odoo",
                "Review and load",
            ],
        )
        self.assertEqual(recipe.stages[2].pages, authoring.viewed_stage.pages)
        self.assertEqual(recipe.stages[2].status, "current")
        self.assertEqual(recipe.viewed_page_label, "Review results")


class ComparisonProgressNavigationTests(unittest.TestCase):
    def test_a_finished_job_does_not_claim_the_review_is_ready_to_load(self):
        for status in PreflightJobStatus:
            for recipe in (False, True):
                with self.subTest(status=status, recipe=recipe):
                    job = _job(status, recipe=recipe)
                    navigation = build_preflight_workspace_navigation(job)
                    stage = navigation.viewed_stage
                    self.assertNotEqual(stage.status, "complete")
                    self.assertEqual(
                        [page.label for page in stage.pages],
                        [
                            "Prepared data",
                            "Compare with Odoo",
                            "Review results",
                        ],
                    )
                    self.assertEqual(
                        [page.label for page in stage.pages if page.current],
                        [
                            "Compare with Odoo",
                        ],
                    )
                    self.assertEqual(
                        stage.pages[1].href,
                        f"/workspaces/{job.workspace_id}/preflight/{job.job_id}",
                    )
                    self.assertNotEqual(stage.pages[2].status, "complete")
                    if not recipe:
                        self.assertIsNone(navigation.stages[-1].href)


class TransferReviewLocationTests(unittest.TestCase):
    def test_transfer_locations_preserve_exact_package_approval_as_a_separate_step(
        self,
    ):
        workspace, selection, schema = _stage_five_state()
        facts = transfer_facts(workspace, selection, schema)
        for step, template, label in (
            ("order", "workspace_transfer_order.html", "Transfer order"),
            ("contents", "workspace_transfer_review.html", "Transfer contents"),
            ("approve", "workspace_transfer_review.html", "Approve transfer"),
        ):
            with self.subTest(step=step):
                navigation = build_workspace_navigation(
                    facts,
                    workspace,
                    template,
                    transfer_review_step=step,
                )
                pages = navigation.viewed_stage.pages
                self.assertEqual(
                    [page.label for page in pages],
                    [
                        "Transfer order",
                        "Transfer contents",
                        "Approve transfer",
                    ],
                )
                self.assertEqual(
                    [page.label for page in pages if page.current], [label]
                )
                self.assertEqual(
                    pages[1].href,
                    f"/workspaces/{workspace.workspace_id}/transfer-review?step=contents",
                )
                self.assertEqual(
                    pages[2].href,
                    f"/workspaces/{workspace.workspace_id}/transfer-review?step=approve",
                )
                self.assertNotEqual(navigation.viewed_stage.status, "complete")
                self.assertIsNone(workspace.transfer_review_approval)
        detail = build_workspace_navigation(
            facts,
            workspace,
            "workspace_transfer_row_review.html",
        )
        self.assertEqual(detail.viewed_stage_id, "transfer-review")
        self.assertTrue(detail.viewed_stage.pages[1].current)


def _job(status: PreflightJobStatus, *, recipe: bool = False) -> PreflightJob:
    now = datetime.now(UTC)
    workspace_id = str(uuid4())
    return PreflightJob(
        job_id=str(uuid4()),
        access_context=WorkspaceAccessContext(
            project_id=str(uuid4()),
            workspace_id=workspace_id,
            data_version_id=str(uuid4()),
            migration_run_id=str(uuid4()),
            recipe_application_id=str(uuid4()) if recipe else None,
        ),
        workspace_id=workspace_id,
        migration_project_name="Products",
        status=status,
        phase=PreflightPhase.COMPLETE,
        message="Saved",
        progress_percent=100,
        attempt=1,
        created_at=now,
        started_at=now,
        updated_at=now,
        finished_at=now,
        preflight_run_id="saved",
        redirect_url="/summary",
        completion_message="Saved",
        failure=None,
    )


if __name__ == "__main__":
    unittest.main()
