"""Keep Recipe setup navigation aligned with accepted data and activation."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.application.workspace.access import WorkspaceAccessContext
from impodo.application.workspace.odoo_capture_jobs import (
    OdooCaptureJob,
    OdooCaptureJobStatus,
    OdooCapturePhase,
)
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.run.models import MigrationRunPurpose
from impodo.web.presenters.navigation import (
    WorkflowStage,
    WorkspaceNavigation,
    build_odoo_capture_workspace_navigation,
    build_recipe_run_navigation,
    _recipe_run_setup_navigation,
)


class RunSetupNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = str(uuid4())
        self.project_id = str(uuid4())
        self.run_id = str(uuid4())
        self.navigation = WorkspaceNavigation(
            workspace_id=self.workspace_id,
            migration_project_name="Customer balances",
            registered=True,
            setup_active=False,
            setup_href=f"/workspaces/{self.workspace_id}/overview",
            overview_href=None,
            overview_active=False,
            current_stage_id="odoo",
            current_stage_label="Odoo data",
            viewed_stage_id="odoo",
            viewed_page_label="Project setup",
            stages=(
                WorkflowStage(
                    stage_id="odoo",
                    number=2,
                    label="Odoo data",
                    href=f"/workspaces/{self.workspace_id}/schema",
                    status="complete",
                    status_label="Complete",
                ),
            ),
        )

    def _render(
        self, purpose, *, fresh_complete=True, activated=False,
        template_name="project_run_fresh_data.html",
    ):
        view = SimpleNamespace(
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            migration_run_id=self.run_id,
            migration_run=SimpleNamespace(purpose=purpose),
            data_version=SimpleNamespace(state=DataVersionState.FROZEN),
        )
        return _recipe_run_setup_navigation(
            self.navigation,
            view,
            template_name=template_name,
            run_setup_complete=activated,
            fresh_data_complete=fresh_complete,
        )

    def test_fresh_data_renders_for_each_run_purpose_with_its_own_setup_link(self):
        for purpose, run_kind in (
            (MigrationRunPurpose.TEST, "test-runs"),
            (MigrationRunPurpose.PRODUCTION, "production-runs"),
        ):
            with self.subTest(purpose=purpose):
                result = self._render(purpose, fresh_complete=False)
                expected = (
                    f"/projects/{self.project_id}/{run_kind}/"
                    f"{self.run_id}/fresh-data"
                )
                self.assertEqual(result.setup_href, expected)
                self.assertEqual(result.viewed_stage_id, "fresh")
                self.assertEqual(result.current_stage_id, "fresh")
                self.assertEqual(result.stages[0].href, expected)
                self.assertTrue(result.stages[0].active)

    def test_run_pages_use_recipe_breadcrumb_labels(self):
        for template_name, expected in (
            ("project_run_fresh_data.html", "Fresh data"),
            ("workspace_schema.html", "Review Odoo requirements"),
            ("project_production_activation.html", "Review Production readiness"),
        ):
            with self.subTest(template=template_name):
                result = self._render(
                    MigrationRunPurpose.PRODUCTION, template_name=template_name,
                )
                self.assertEqual(result.viewed_page_label, expected)

    def test_missing_answers_lock_odoo_even_when_the_data_version_is_frozen(self):
        result = self._render(
            MigrationRunPurpose.PRODUCTION, fresh_complete=False,
        )

        for stage in result.stages[1:]:
            self.assertEqual(stage.status, "locked")
            self.assertIsNone(stage.href)

    def test_accepted_production_data_opens_odoo_but_activation_gates_review(self):
        result = self._render(MigrationRunPurpose.PRODUCTION)

        self.assertEqual(result.current_stage_id, "odoo")
        self.assertEqual(result.stages[1].status, "current")
        self.assertEqual(
            result.stages[1].href,
            f"/projects/{self.project_id}/runs/{self.run_id}/odoo",
        )
        self.assertEqual(result.stages[2].status, "locked")
        self.assertIsNone(result.stages[2].href)

    def test_activated_production_opens_review_and_load(self):
        result = self._render(MigrationRunPurpose.PRODUCTION, activated=True)

        self.assertEqual(result.current_stage_id, "review")
        self.assertEqual(result.stages[1].status, "complete")
        self.assertEqual(result.stages[2].status, "current")
        self.assertEqual(
            result.stages[2].href,
            f"/projects/{self.project_id}/runs/{self.run_id}",
        )

    def test_run_overview_keeps_both_purposes_in_their_own_recipe_journey(self):
        for purpose, kind in (("TEST", "test-runs"), ("PRODUCTION", "production-runs")):
            with self.subTest(purpose=purpose):
                navigation = build_recipe_run_navigation(
                    project_id=self.project_id, migration_run_id=self.run_id,
                    migration_project_name="Customer balances", run_purpose=purpose,
                    complete=False, odoo_needs_attention=False,
                )
                self.assertFalse(hasattr(navigation, "workspace_id"))
                self.assertEqual(navigation.journey_label, "Recipe run")
                self.assertEqual(navigation.current_stage_id, "review")
                self.assertEqual(navigation.viewed_stage_id, "review")
                self.assertEqual(navigation.stages[0].href,
                    f"/projects/{self.project_id}/{kind}/{self.run_id}/fresh-data")
                self.assertEqual(navigation.stages[1].href,
                    f"/projects/{self.project_id}/runs/{self.run_id}/odoo")
                self.assertEqual(navigation.stages[2].status, "current")

    def test_run_navigation_distinguishes_odoo_attention_and_verified_results(self):
        for complete, attention, expected_stage, expected_status in (
            (False, True, "odoo", "current"),
            (True, False, "review", "complete"),
        ):
            with self.subTest(complete=complete, attention=attention):
                navigation = build_recipe_run_navigation(
                    project_id=self.project_id, migration_run_id=self.run_id,
                    migration_project_name="Customer balances", run_purpose="TEST",
                    complete=complete, odoo_needs_attention=attention,
                )
                self.assertEqual(navigation.current_stage_id, expected_stage)
                self.assertEqual(navigation.stages[1].status,
                    "attention" if attention else "complete")
                self.assertEqual(navigation.stages[2].status, expected_status)
                self.assertTrue(navigation.stages[2].active)


class OdooCaptureNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = str(uuid4())
        self.project_id = str(uuid4())
        self.run_id = str(uuid4())
        self.job = OdooCaptureJob(
            job_id=str(uuid4()),
            access_context=WorkspaceAccessContext(
                project_id=self.project_id,
                workspace_id=self.workspace_id,
                data_version_id=str(uuid4()),
                migration_run_id=self.run_id,
            ),
            workspace_id=self.workspace_id,
            migration_project_name="Products",
            status=OdooCaptureJobStatus.RUNNING,
            phase=OdooCapturePhase.READING,
            message="Reading the selected Odoo records",
            completed_rows=10,
            total_rows=20,
            page_count=1,
            relationship_check_page_count=0,
            response_bytes=100,
            normalized_bytes=80,
            progress_percent=44,
            attempt=1,
            cancel_requested=False,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            finished_at=None,
            manifest_id="",
            failure_message="",
        )

    def test_running_capture_keeps_the_six_stage_sidebar_visible(self) -> None:
        navigation = build_odoo_capture_workspace_navigation(self.job)

        self.assertEqual(len(navigation.stages), 6)
        self.assertEqual(navigation.current_stage_id, "download")
        self.assertEqual(navigation.viewed_stage_id, "download")
        self.assertEqual(navigation.stages[2].status, "current")
        self.assertTrue(navigation.stages[2].active)
        self.assertTrue(navigation.stages[2].pages[1].current)
        self.assertEqual(
            [stage.label for stage in navigation.stages[:4]],
            [
                "Connect source Odoo",
                "Select data to download",
                "Download and freeze",
                "Connect and match destination",
            ],
        )

    def test_completed_capture_opens_the_destination_stage(self) -> None:
        navigation = build_odoo_capture_workspace_navigation(
            replace(self.job, status=OdooCaptureJobStatus.SUCCEEDED)
        )

        self.assertEqual(navigation.stages[2].status, "complete")
        self.assertEqual(navigation.current_stage_id, "destination")
        self.assertEqual(navigation.stages[3].status, "current")
        self.assertEqual(
            navigation.stages[3].href,
            f"/workspaces/{self.workspace_id}/transfer-destination",
        )


if __name__ == "__main__":
    unittest.main()
