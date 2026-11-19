"""Project landing actions follow the current run and its own verified evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.correction_workflow import CorrectionJourneyView
from impodo.application.workspace.execution.navigation import (
    ExecutionNavigationState,
    ExecutionPreviewSummary,
)
from impodo.application.workspace.navigation import (
    ProjectLoadOutcome,
    WorkspaceNavigationFacts,
    WorkspaceNavigationSnapshot,
)
from impodo.domain.data_version.models import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
)
from impodo.domain.execution.models import ExecutionRowStatus
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProject,
    MigrationProjectStatus,
)
from impodo.domain.run.models import (
    MigrationRun,
    MigrationRunPurpose,
    MigrationRunState,
)
from impodo.domain.workspace.models import MigrationWorkspace, MigrationWorkspaceState
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateNotFoundError,
    WorkspaceStatus,
)
from impodo.web.presenters.project_overview import project_overview_action


class ProjectOverviewActionTests(TestCase):
    def setUp(self):
        now = datetime(2026, 11, 19, tzinfo=UTC)
        self.project = MigrationProject(
            str(uuid4()),
            "Customers",
            "Import customers",
            "Odoo",
            MigrationDataClassification.INTERNAL,
            365,
            MigrationProjectStatus.ACTIVE,
            1,
            now,
            now,
        )
        self.version = DataVersion(
            str(uuid4()),
            self.project.project_id,
            1,
            None,
            DataVersionPurpose.AUTHORING,
            DataVersionState.DRAFT,
            "Customers",
            "",
            None,
            1,
            now,
            now,
        )
        self.run = MigrationRun(
            str(uuid4()),
            self.project.project_id,
            self.version.data_version_id,
            1,
            MigrationRunPurpose.AUTHORING,
            "Customers",
            MigrationRunState.DRAFT,
            None,
            None,
            1,
            now,
            now,
        )
        self.owner = MigrationWorkspace(
            str(uuid4()),
            self.project.project_id,
            self.version.data_version_id,
            self.run.migration_run_id,
            None,
            "Customers",
            MigrationWorkspaceState.OPEN,
            1,
            now,
            now,
        )
        self.workspace = WorkspaceState(
            self.owner.workspace_id,
            "Customers",
            "Odoo",
            status=WorkspaceStatus.REGISTERED,
        )
        self.facts = WorkspaceNavigationFacts(self.owner.workspace_id)
        self.reader = Mock()
        self.hash = "sha256:" + "a" * 64
        self.execution = ExecutionNavigationState(
            ExecutionPreviewSummary(
                "review1",
                self.hash,
                self.hash,
                "READY",
                11,
                3,
                5,
                0,
                0,
                0,
                self.hash,
                "20.0",
                "",
                "",
                "",
                "",
                True,
            ),
            "load1",
            "COMPLETED",
            "VERIFIED",
            "verification1",
            now,
            "odoo-test",
            True,
        )

    def render(self, **overrides):
        self.reader.return_value = WorkspaceNavigationSnapshot(
            self.workspace, self.facts
        )
        options = dict(
            project=self.project,
            runs=(self.run,),
            workspaces=(self.owner,),
            authoring_workspace=self.owner,
            authoring_data_version=self.version,
            test_bindings={},
            production_bindings=(),
            completed_production_setups=(),
            correction_journeys=(),
            workspace_snapshot=self.reader,
        )
        return project_overview_action(**(options | overrides))

    def test_saved_stage_and_attention_use_stage_navigation(self):
        self.facts = replace(
            self.facts,
            source_selection_hash=self.hash,
            schema_present=True,
            governance_present=True,
        )
        matching = self.render()
        self.assertEqual(matching.detail, "Stage 3 of 6")
        self.assertEqual(
            matching.href, f"/workspaces/{self.owner.workspace_id}/mapping"
        )
        self.facts = replace(
            self.facts, schema_attention=True, governance_present=False
        )
        blocked = self.render()
        self.assertEqual(blocked.status_label, "Needs attention")
        self.assertIn(f"/workspaces/{self.owner.workspace_id}/schema", blocked.href)

    def test_job_reopens_progress_and_revision_reopens_its_own_comparison(self):
        self.facts = replace(self.facts, active_preparation_job_id="job1")
        self.assertEqual(
            self.render().href,
            f"/workspaces/{self.owner.workspace_id}/preparation/job1",
        )
        self.facts = replace(
            self.facts, active_preparation_job_id="", active_load_job_id="loadJob"
        )
        self.assertEqual(
            self.render().href,
            f"/workspaces/{self.owner.workspace_id}/load/progress/loadJob",
        )
        self.facts = replace(self.facts, active_load_job_id="")
        correction = CorrectionJourneyView(
            self.project.project_id,
            "original",
            self.owner.workspace_id,
            True,
            False,
            False,
            None,
        )
        revision = self.render(correction_journeys=(correction,))
        self.assertIn("Working revision", revision.detail)
        self.assertEqual(
            revision.href, f"/workspaces/{self.owner.workspace_id}/continuation/confirm"
        )

    def test_verified_open_or_sealed_load_has_bound_summary_and_workbook(self):
        self.facts = replace(self.facts, execution_state=self.execution)
        self.workspace = replace(
            self.workspace, odoo_company=OdooCompanySelection(self.hash, 1, "Wiltz")
        )
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                closed = replace(self.owner, state=MigrationWorkspaceState.CLOSED)
                overrides = (
                    dict(
                        workspaces=(closed,),
                        authoring_workspace=None,
                        authoring_data_version=None,
                    )
                    if sealed
                    else {}
                )
                result = self.render(**overrides)
                self.assertEqual(result.title, "Load verified")
                self.assertEqual(
                    result.detail,
                    "odoo-test · Wiltz · 19 Nov 2026 · 11 created · 3 updated · 5 unchanged",
                )
                self.assertEqual(
                    result.href, f"/workspaces/{self.owner.workspace_id}/load/outcome"
                )
                self.assertEqual(
                    result.workbook_href,
                    f"/workspaces/{self.owner.workspace_id}/load/fallout.xlsx?verification=verification1",
                )

    def test_no_changes_and_missing_workbook_have_distinct_display(self):
        summary = replace(self.execution.summary, create_count=0, update_count=0)
        self.facts = replace(
            self.facts,
            execution_state=replace(
                self.execution, summary=summary, has_workbook_detail=False
            ),
        )
        self.workspace = replace(
            self.workspace,
            odoo_company=OdooCompanySelection(
                "sha256:" + "b" * 64, 1, "Another database"
            ),
        )
        result = self.render()
        self.assertEqual(result.title, "No changes needed")
        self.assertNotIn("Another database", result.detail)
        self.assertIsNone(result.workbook_href)

    def test_partial_or_unverified_results_are_not_successful(self):
        for status in ("", "FALLOUT", "OUTCOME_UNKNOWN"):
            with self.subTest(status=status):
                self.facts = replace(
                    self.facts,
                    execution_state=replace(
                        self.execution,
                        reconciliation_status=status,
                        has_workbook_detail=False,
                    ),
                )
                result = self.render()
                self.assertEqual(result.status_label, "Needs attention")
                self.assertNotIn("verified", result.title.lower())
                self.assertIn("/load/outcome", result.href)
                self.assertIsNone(result.workbook_href)

    def test_transfer_uses_its_destination_result_only(self):
        self.workspace = replace(
            self.workspace,
            source_mode=SourceMode.ODOO,
            destination_odoo_database="odoo-production",
        )
        self.facts = replace(
            self.facts,
            transfer_execution_run_id="transfer",
            transfer_reconciliation_status="FALLOUT",
            execution_state=self.execution,
        )
        self.assertEqual(self.render().status, "attention")
        self.facts = replace(self.facts, transfer_reconciliation_status="VERIFIED")
        result = self.render()
        self.assertEqual(result.detail, "odoo-production")
        self.assertEqual(
            result.href, f"/workspaces/{self.owner.workspace_id}/transfer-load/outcome"
        )
        self.assertIsNone(result.workbook_href)

    def test_active_recipe_run_takes_precedence_over_history_without_opening_workspaces(
        self,
    ):
        for purpose in (MigrationRunPurpose.TEST, MigrationRunPurpose.PRODUCTION):
            with self.subTest(purpose=purpose):
                run = replace(
                    self.run,
                    migration_run_id=str(uuid4()),
                    run_number=2,
                    purpose=purpose,
                    state=MigrationRunState.READY,
                    updated_at=self.run.updated_at + timedelta(days=1),
                )
                result = self.render(runs=(self.run, run))
                self.assertEqual(result.status_label, "In progress")
                self.assertEqual(
                    result.href,
                    f"/projects/{self.project.project_id}/runs/{run.migration_run_id}",
                )
        self.reader.assert_not_called()

    def test_closed_projects_do_not_offer_authoring(self):
        for state in (MigrationProjectStatus.CLOSED, MigrationProjectStatus.ARCHIVED):
            with self.subTest(state=state):
                result = self.render(project=replace(self.project, status=state))
                self.assertEqual(result.status_label, state.value.capitalize())
                self.assertIsNone(result.href)
        self.reader.assert_not_called()

    def test_draft_uses_source_package_header_and_preserves_saved_files(self):
        for mode, has_files, label, suffix, backup_eligible in (
            (SourceMode.FILE, False, "Add source files", "files", True),
            (SourceMode.FILE, True, "Review source files", "files", False),
            (SourceMode.ODOO, False, "Connect source Odoo", "target", True),
        ):
            with self.subTest(mode=mode, has_files=has_files):
                action = self.render(draft_source=lambda _version, _workspace: (mode, has_files))
                self.assertEqual(
                    action.href, f"/workspaces/{self.owner.workspace_id}/{suffix}"
                )
                self.assertEqual(action.action_label, label)
                self.assertEqual(action.saved_data_candidate, backup_eligible)
        self.assertIsNone(self.render(draft_source=lambda _version, _workspace: None).href)
        self.reader.assert_not_called()

    def test_missing_workspace_has_no_dead_link(self):
        self.reader.side_effect = WorkspaceStateNotFoundError("Workspace missing")
        result = self.render()
        self.assertEqual(result.status, "attention")
        self.assertIsNone(result.href)

    def test_capture_transfer_and_comparison_link_to_the_owning_job(self):
        jobs = Mock()
        self.facts = replace(self.facts, normalization_status="FROZEN")
        jobs.preflight_jobs.active.return_value.job_id = "comparison"
        self.assertEqual(
            self.render(active_jobs=jobs).href,
            f"/workspaces/{self.owner.workspace_id}/preflight/comparison",
        )
        jobs.preflight_jobs.active.assert_called_once_with(
            self.owner.workspace_id, session_only=True
        )
        self.workspace = replace(self.workspace, source_mode=SourceMode.ODOO)
        jobs.odoo_capture_jobs.active.return_value.job_id = "capture"
        self.assertEqual(
            self.render(active_jobs=jobs).href,
            f"/workspaces/{self.owner.workspace_id}/sources/odoo-capture/capture",
        )
        self.facts = replace(self.facts, active_load_job_id="transfer")
        self.assertEqual(
            self.render(active_jobs=jobs).href,
            f"/workspaces/{self.owner.workspace_id}/transfer-load/progress/transfer",
        )
        self.assertEqual(jobs.odoo_capture_jobs.active.call_count, 1)

    def test_partial_summary_uses_recorded_destination_and_actual_journal_counts(self):
        outcome = ProjectLoadOutcome(
            "actual-db",
            "Wiltz",
            None,
            (
                (ExecutionRowStatus.COMMITTED, 7),
                (ExecutionRowStatus.FAILED, 1),
                (ExecutionRowStatus.BLOCKED, 5),
            ),
        )
        self.facts = replace(
            self.facts,
            execution_state=replace(self.execution, reconciliation_status="FALLOUT"),
            project_load_outcome=outcome,
        )
        result = self.render()
        self.assertEqual(
            result.detail,
            "actual-db · Wiltz · 7 written · 1 rejected · 5 not attempted",
        )
        self.assertEqual(result.workbook_label, "Download highlighted source workbook")
        self.workspace = replace(self.workspace, source_mode=SourceMode.ODOO)
        self.facts = replace(self.facts, transfer_execution_run_id="transfer")
        self.assertEqual(self.render().detail, result.detail)

    def test_protected_results_hide_location_counts_and_workbook_for_view_only_access(
        self,
    ):
        self.facts = replace(self.facts, execution_state=self.execution)
        for sealed in (False, True):
            with self.subTest(sealed=sealed):
                closed = replace(self.owner, state=MigrationWorkspaceState.CLOSED)
                overrides = (
                    dict(
                        workspaces=(closed,),
                        authoring_workspace=None,
                        authoring_data_version=None,
                    )
                    if sealed
                    else {}
                )
                action = self.render(protected_results=False, **overrides)
                self.assertEqual(action.title, "Load verified")
                self.assertEqual(action.detail, "")
                self.assertIsNone(action.href)
                self.assertIsNone(action.workbook_href)
        self.facts = replace(self.facts, execution_state=None)
        history = self.render(
            protected_results=False,
            workspaces=(closed,),
            authoring_workspace=None,
            authoring_data_version=None,
        )
        self.assertIsNone(history.href)
