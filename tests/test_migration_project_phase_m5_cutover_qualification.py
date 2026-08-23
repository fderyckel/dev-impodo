"""Verify M5 exact integrated qualification and rollout selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.access import LOCAL_ACTOR
from impodo.application.cutover_plan_service import CutoverPlanService
from impodo.domain.reconciliation import ReconciliationRunStatus
from impodo.domain.serialization import content_hash
from impodo.migration_cutover import (
    ApplicationQualificationEvidence,
    CutoverQualificationState,
    QualifiedOutcomes,
)
from impodo.migration_foundation import MigrationConflictError
from impodo.migration_run_planning import RecipeApplicationStatus
from impodo.web.app import create_local_app
from tests import test_migration_project_phase_m4_multi_recipe_runs as m4


class CompleteEvidenceReader:
    """Supply exact frozen evidence without opening Odoo or doing row work."""

    def __init__(self, upstream_recipe_id: str) -> None:
        self.upstream_recipe_id = upstream_recipe_id
        self.predecessor_verified = True

    def read(self, application, *, target_binding_hash):
        base = datetime(2026, 8, 22, 8, tzinfo=timezone.utc)
        if application.recipe_id == self.upstream_recipe_id:
            started_at = base
            completed_at = base + timedelta(minutes=1)
            reconciled_at = base + timedelta(minutes=2)
        else:
            started_at = base + timedelta(minutes=3)
            completed_at = base + timedelta(minutes=4)
            reconciled_at = base + timedelta(minutes=5)
        outcomes = QualifiedOutcomes(2, 1, 3, 6)
        hashes = {
            name: content_hash(f"{application.application_id}:{name}")
            for name in (
                "preparation",
                "quality",
                "comparison",
                "execution",
                "read-back",
                "reconciliation",
                "control",
            )
        }
        unhashed = {
            "application_id": application.application_id,
            "comparison_hash": hashes["comparison"],
            "contract_version": 1,
            "control_hash": hashes["control"],
            "execution_completed_at": completed_at.isoformat(),
            "execution_hash": hashes["execution"],
            "execution_started_at": started_at.isoformat(),
            "mapping_content_hash": application.mapping_content_hash,
            "migration_run_id": application.migration_run_id,
            "outcomes": outcomes.to_dict(),
            "preparation_hash": hashes["preparation"],
            "project_id": application.project_id,
            "quality_hash": hashes["quality"],
            "read_back_hash": hashes["read-back"],
            "recipe_id": application.recipe_id,
            "recipe_revision": application.recipe_revision,
            "recipe_semantic_hash": application.recipe_semantic_hash,
            "reconciled_at": reconciled_at.isoformat(),
            "reconciliation_hash": hashes["reconciliation"],
            "target_binding_hash": target_binding_hash,
            "workspace_id": application.workspace_id,
        }
        return (
            ApplicationQualificationEvidence(
                application_id=application.application_id,
                project_id=application.project_id,
                migration_run_id=application.migration_run_id,
                workspace_id=application.workspace_id,
                recipe_id=application.recipe_id,
                recipe_revision=application.recipe_revision,
                recipe_semantic_hash=application.recipe_semantic_hash,
                target_binding_hash=target_binding_hash,
                mapping_content_hash=str(application.mapping_content_hash),
                preparation_hash=hashes["preparation"],
                quality_hash=hashes["quality"],
                comparison_hash=hashes["comparison"],
                execution_hash=hashes["execution"],
                read_back_hash=hashes["read-back"],
                reconciliation_hash=hashes["reconciliation"],
                control_hash=hashes["control"],
                outcomes=outcomes,
                execution_started_at=started_at,
                execution_completed_at=completed_at,
                reconciled_at=reconciled_at,
                content_hash=content_hash(unhashed),
            ),
            (),
        )

    def reconciliation_current(self, _workspace_id):
        return SimpleNamespace(
            status=(
                ReconciliationRunStatus.VERIFIED
                if self.predecessor_verified
                else ReconciliationRunStatus.FALLOUT
            ),
            fallout_count=0 if self.predecessor_verified else 1,
            unknown_count=0,
        )


class MigrationProjectPhaseM5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = m4.MigrationProjectPhaseM4Tests(
            methodName="test_two_recipes_share_one_run_target_and_keep_isolated_workspaces"
        )
        self.fixture.setUp()
        self.reader = CompleteEvidenceReader(
            self.fixture.customer.recipe.recipe_id
        )
        self.service = CutoverPlanService(
            projects=self.fixture.projects,
            data_versions=self.fixture.data_versions,
            run_planning=self.fixture.planning_repository,
            repository=self.fixture.cutover_repository,
            evidence_reader=self.reader,
            authorization=self.fixture.authorization,
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_plan_qualification_and_selection_pin_exact_test_evidence(self):
        run = self.fixture._start()
        binding = self.fixture.cutover_repository.get_run_binding(
            run.run.migration_run_id
        )
        self.assertEqual(binding.cutover_plan_revision, 1)
        self.assertEqual(
            self.fixture.cutover_repository.list_qualifications(
                binding.cutover_plan_id,
                binding.cutover_plan_revision,
            ),
            (),
        )

        review = self.service.review(
            run.run.project_id,
            run.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(review.state, CutoverQualificationState.READY)
        self.assertTrue(review.can_qualify)
        project = self.fixture.projects.get(run.run.project_id, actor=LOCAL_ACTOR)
        qualification_operation_id = str(uuid4())
        qualification = self.service.qualify(
            run.run.project_id,
            run.run.migration_run_id,
            expected_workspace_revision=project.optimistic_revision,
            expected_evidence_hash=str(review.integrated_evidence_hash),
            operation_id=qualification_operation_id,
            actor=LOCAL_ACTOR,
        )
        replayed_qualification = self.service.qualify(
            run.run.project_id,
            run.run.migration_run_id,
            expected_workspace_revision=project.optimistic_revision,
            expected_evidence_hash=str(review.integrated_evidence_hash),
            operation_id=qualification_operation_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            replayed_qualification.qualification_id,
            qualification.qualification_id,
        )
        self.assertEqual(qualification.cutover_plan_revision, 1)
        self.assertEqual(len(qualification.application_ids), 2)
        self.assertTrue(
            qualification.evidence_storage_key.startswith(run.run.project_id)
        )
        stored = self.fixture.planning_repository.list_applications(
            run.run.migration_run_id
        )
        self.assertEqual(
            {item.status for item in stored},
            {RecipeApplicationStatus.QUALIFIED},
        )

        project = self.fixture.projects.get(run.run.project_id, actor=LOCAL_ACTOR)
        selection_operation_id = str(uuid4())
        selection = self.service.select(
            run.run.project_id,
            qualification.qualification_id,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=selection_operation_id,
            actor=LOCAL_ACTOR,
        )
        replayed_selection = self.service.select(
            run.run.project_id,
            qualification.qualification_id,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=selection_operation_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            replayed_selection.cutover_selection_id,
            selection.cutover_selection_id,
        )
        self.assertEqual(selection.qualification_id, qualification.qualification_id)
        self.assertIsNone(
            self.fixture.runs.get(run.run.migration_run_id, actor=LOCAL_ACTOR).cutover_selection_id
        )

    def test_changed_dependency_appends_an_unqualified_plan_revision(self):
        first = self.fixture._start()
        review = self.service.review(
            first.run.project_id,
            first.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        project = self.fixture.projects.get(first.run.project_id, actor=LOCAL_ACTOR)
        self.service.qualify(
            first.run.project_id,
            first.run.migration_run_id,
            expected_workspace_revision=project.optimistic_revision,
            expected_evidence_hash=str(review.integrated_evidence_hash),
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        project = self.fixture.projects.get(first.run.project_id, actor=LOCAL_ACTOR)
        second = self.fixture.planning.start_test_run(
            first.run.project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=self.fixture.test_data_version.data_version_id,
            recipe_revisions=self.fixture._selected(),
            dependencies=(),
            target_schema=self.fixture.schema,
            target_reference_bundle=None,
            credential_generation=self.fixture.schema.read_credential_binding_hash,
            label="Integrated Test without dependency",
            operation_id=str(uuid4()),
            actor=LOCAL_ACTOR,
        )
        binding = self.fixture.cutover_repository.get_run_binding(
            second.run.migration_run_id
        )
        self.assertEqual(binding.cutover_plan_revision, 2)
        self.assertEqual(
            self.fixture.cutover_repository.list_qualifications(
                binding.cutover_plan_id,
                binding.cutover_plan_revision,
            ),
            (),
        )

    def test_qualification_recovers_after_protected_evidence_fault(self):
        run = self.fixture._start()
        review = self.service.review(
            run.run.project_id,
            run.run.migration_run_id,
            actor=LOCAL_ACTOR,
        )
        project = self.fixture.projects.get(run.run.project_id, actor=LOCAL_ACTOR)
        operation_id = str(uuid4())

        def fault(stage):
            if stage == "EVIDENCE_STORED":
                raise m4.SimulatedCrash(stage)

        with self.assertRaises(m4.SimulatedCrash):
            self.service.qualify(
                run.run.project_id,
                run.run.migration_run_id,
                expected_workspace_revision=project.optimistic_revision,
                expected_evidence_hash=str(review.integrated_evidence_hash),
                operation_id=operation_id,
                actor=LOCAL_ACTOR,
                fault=fault,
            )
        recovered = self.service.qualify(
            run.run.project_id,
            run.run.migration_run_id,
            expected_workspace_revision=project.optimistic_revision,
            expected_evidence_hash=str(review.integrated_evidence_hash),
            operation_id=operation_id,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(
            len(
                self.fixture.cutover_repository.list_qualifications(
                    recovered.cutover_plan_id,
                    recovered.cutover_plan_revision,
                )
            ),
            1,
        )

    def test_downstream_write_is_blocked_until_upstream_is_reconciled(self):
        run = self.fixture._start()
        downstream = next(
            item
            for item in run.applications
            if item.recipe_id == self.fixture.product.recipe.recipe_id
        )
        self.reader.predecessor_verified = False
        with self.assertRaises(MigrationConflictError):
            self.service.assert_application_can_execute(
                downstream.workspace_id,
                actor=LOCAL_ACTOR,
            )
        self.reader.predecessor_verified = True
        self.service.assert_application_can_execute(
            downstream.workspace_id,
            actor=LOCAL_ACTOR,
        )

    def test_browser_explains_incomplete_integrated_evidence(self):
        run = self.fixture._start()
        app = create_local_app(
            self.fixture.root,
            launch_token="m5-launch",
            session_secret="m5-session",
            secret_store=self.fixture.secret_store,
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        with TestClient(app) as client:
            self.assertEqual(
                client.get(
                    "/launch?token=m5-launch",
                    follow_redirects=False,
                ).status_code,
                303,
            )
            response = client.get(
                f"/projects/{run.run.project_id}/runs/"
                f"{run.run.migration_run_id}/qualification"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Integrated Test qualification", response.text)
        self.assertIn("Evidence is not complete", response.text)
        self.assertIn("Production", response.text)


if __name__ == "__main__":
    unittest.main()

