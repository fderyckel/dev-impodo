"""Verify exact R4 Test qualification and rollout-candidate behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.application.recipe_qualification_service import (
    RecipeQualificationService,
)
from impodo.domain.execution import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.recipe_applications import (
    RecipeApplicationDraft,
    RecipeApplicationEvidence,
    RecipeApplicationIssue,
    RecipeApplicationIssueLevel,
    RecipeApplicationState,
    TargetBinding,
    TargetCredentialRole,
    TargetEnvironment,
    TargetProbeStatus,
)
from impodo.domain.recipe_qualifications import (
    CutoverCandidateRecord,
    RecipeQualificationError,
    RecipeQualificationRecord,
    RecipeQualificationState,
)
from impodo.domain.reconciliation import (
    ReconciliationRow,
    ReconciliationRowStatus,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from impodo.domain.serialization import content_hash
from impodo.recipes import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
    Recipe,
    RecipeState,
    SetupHydrationState,
)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


class _Recipes:
    def __init__(self, recipe, data_version, semantic_hash) -> None:
        self.recipe = recipe
        self.data_version = data_version
        self.semantic_hash = semantic_hash
        self.qualification = None
        self.candidate = None
        self.published_evidence = None

    def get(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return self.recipe

    def data_versions(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return (self.data_version,)

    def read_revision(self, recipe_id, version, *, actor):
        del actor
        assert (recipe_id, version) == (
            self.recipe.recipe_id,
            self.recipe.current_recipe_revision,
        )
        return {"semantic_hash": self.semantic_hash}

    def current_qualification(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        if (
            self.qualification is not None
            and self.qualification.recipe_revision
            == self.recipe.current_recipe_revision
        ):
            return self.qualification
        return None

    def cutover_candidate(self, recipe_id, *, actor):
        del actor
        assert recipe_id == self.recipe.recipe_id
        return self.candidate

    def publish_qualification(
        self,
        recipe_id,
        *,
        expected_recipe_revision,
        evidence,
        actor,
    ):
        assert recipe_id == self.recipe.recipe_id
        assert expected_recipe_revision == self.recipe.optimistic_revision
        self.published_evidence = dict(evidence)
        now = datetime.now(timezone.utc)
        self.qualification = RecipeQualificationRecord(
            qualification_id=str(uuid4()),
            recipe_id=recipe_id,
            recipe_revision=self.recipe.current_recipe_revision,
            application_id=str(evidence["application_id"]),
            test_target_binding_hash=str(evidence["test_target_binding_hash"]),
            status="TEST_QUALIFIED",
            findings=(),
            qualified_by=actor.identity,
            qualified_at=now,
            evidence_storage_key="qualifications/test",
            evidence_hash=content_hash({"evidence": evidence}),
        )
        self.recipe = replace(
            self.recipe,
            optimistic_revision=self.recipe.optimistic_revision + 1,
            updated_at=now,
        )

    def select_cutover_candidate(
        self,
        recipe_id,
        *,
        expected_recipe_revision,
        recipe_revision,
        qualification_id,
        qualification_evidence_hash,
        actor,
    ):
        assert expected_recipe_revision == self.recipe.optimistic_revision
        assert recipe_revision == self.recipe.current_recipe_revision
        assert qualification_id == self.qualification.qualification_id
        assert qualification_evidence_hash == self.qualification.evidence_hash
        now = datetime.now(timezone.utc)
        self.candidate = CutoverCandidateRecord(
            cutover_candidate_id=str(uuid4()),
            recipe_id=recipe_id,
            recipe_revision=recipe_revision,
            qualification_id=qualification_id,
            selected_by=actor.identity,
            selected_at=now,
            content_hash=_hash("f"),
        )


class _RecipeApplications:
    def __init__(self, project, target_binding) -> None:
        self.project_reader = SimpleNamespace(get=lambda _project_id: project)
        self.target_binding = target_binding

    def review(
        self,
        recipe_id,
        *,
        credential_generation,
        credential_storage_class,
        actor,
    ):
        del recipe_id, credential_storage_class, actor
        current = credential_generation == self.target_binding.credential_generation
        issue = RecipeApplicationIssue(
            code="TARGET_BINDING_STALE",
            level=RecipeApplicationIssueLevel.BLOCKER,
            message="The Test read key changed.",
            recovery_action="Refresh Odoo evidence.",
        )
        return SimpleNamespace(
            can_apply=current,
            target_binding=(self.target_binding if current else None),
            issues=(() if current else (issue,)),
        )


class _ApplicationState:
    def __init__(self, draft, evidence, target) -> None:
        self.draft = draft
        self.evidence = evidence
        self.target = target

    def get_draft(self, project_id):
        del project_id
        return self.draft

    def get_evidence(self, project_id, application_id):
        del project_id
        return self.evidence if application_id == self.evidence.application_id else None

    def get_target_binding(self, project_id):
        del project_id
        return self.target


class RecipeQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        now = datetime.now(timezone.utc)
        self.recipe_id = str(uuid4())
        self.project_id = str(uuid4())
        self.data_version_id = str(uuid4())
        self.application_id = str(uuid4())
        self.mapping_id = str(uuid4())
        self.semantic_hash = _hash("1")
        self.mapping_hash = _hash("2")
        self.recipe = Recipe(
            recipe_id=self.recipe_id,
            display_name="Customer migration",
            business_purpose="Move customers",
            state=RecipeState.ACTIVE,
            data_classification="INTERNAL",
            retention_days=90,
            current_recipe_revision=3,
            current_data_version_id=self.data_version_id,
            pending_data_version_id=None,
            cutover_candidate_id=None,
            setup_hydration_state=SetupHydrationState.READY,
            setup_hydration_hash=_hash("3"),
            optimistic_revision=7,
            created_at=now,
            updated_at=now,
        )
        self.data_version = DataVersion(
            data_version_id=self.data_version_id,
            recipe_id=self.recipe_id,
            version_number=4,
            workspace_project_id=self.project_id,
            parent_data_version_id=None,
            purpose=DataVersionPurpose.TEST,
            state=DataVersionState.ACTIVE,
            pinned_recipe_revision=3,
            label="Recipe v3 rehearsal",
            export_as_of_date=None,
            parameter_values_hash=_hash("4"),
            intake_status="READY",
            created_at=now,
            sealed_at=None,
        )
        self.project = SimpleNamespace(
            project_id=self.project_id,
            name="Customer migration Test",
        )
        self.target = TargetBinding(
            target_binding_id=str(uuid4()),
            environment=TargetEnvironment.TEST,
            endpoint="https://test.example.test",
            database="test-v3",
            connection_target_hash=_hash("5"),
            credential_role=TargetCredentialRole.READ,
            credential_generation=_hash("6"),
            credential_storage_class="SESSION",
            principal_hash=_hash("7"),
            permission_hash=_hash("8"),
            context_hash=_hash("9"),
            schema_dependency_hash=_hash("a"),
            reference_snapshot_hashes=(),
            probe_status=TargetProbeStatus.ACCEPTED,
            probed_at=now,
            captured_by=LOCAL_ACTOR.identity,
        )
        self.application = RecipeApplicationEvidence(
            application_id=self.application_id,
            recipe_id=self.recipe_id,
            recipe_revision=3,
            recipe_semantic_hash=self.semantic_hash,
            data_version_id=self.data_version_id,
            workspace_project_id=self.project_id,
            source_artifact_hash=_hash("b"),
            source_selection_hash=_hash("c"),
            parameter_values_hash=_hash("4"),
            control_values_hash=_hash("d"),
            target_binding_id=self.target.target_binding_id,
            target_binding_hash=self.target.content_hash,
            target_contract_assessment_hash=_hash("a"),
            binding_hash=_hash("e"),
            issue_hash=_hash("f"),
            mapping_id=self.mapping_id,
            mapping_content_hash=self.mapping_hash,
            status=RecipeApplicationState.APPLIED,
            created_at=now,
            created_by=LOCAL_ACTOR.identity,
        )
        self.draft = RecipeApplicationDraft(
            application_id=self.application_id,
            recipe_id=self.recipe_id,
            recipe_revision=3,
            data_version_id=self.data_version_id,
            workspace_project_id=self.project_id,
            target_binding_hash=self.target.content_hash,
            source_selection_hash=self.application.source_selection_hash,
            parameter_values_hash=self.application.parameter_values_hash,
            revision=2,
            state=RecipeApplicationState.APPLIED,
            overrides={},
            issues=(),
            binding_hash=self.application.binding_hash,
            target_assessment_hash=self.application.target_contract_assessment_hash,
            updated_at=now,
            updated_by=LOCAL_ACTOR.identity,
        )
        self.staging = SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=_hash("0"),
            mapping_id=self.mapping_id,
            mapping_version=1,
            attention_rows=0,
            control_totals_passed=True,
            control_totals=(),
        )
        self.quality = SimpleNamespace(
            run_id=str(uuid4()),
            content_hash=_hash("1"),
            staging_run_id=self.staging.run_id,
            staging_content_hash=self.staging.content_hash,
            ready_for_package=True,
        )
        self.report = SimpleNamespace(
            run_id=str(uuid4()),
            status="READY",
            attention_count=0,
            create_count=1,
            update_count=1,
            unchanged_count=1,
            total_count=3,
            to_json=lambda: json.dumps(
                {
                    "run_id": "comparison",
                    "counts": {"CREATE": 1, "UPDATE": 1, "UNCHANGED": 1},
                }
            ),
        )
        self.snapshot = SimpleNamespace(
            preflight_run_id=self.report.run_id,
            semantic_hash=_hash("2"),
            counts={"CREATE": 1, "UPDATE": 1, "UNCHANGED": 1},
        )
        rows = tuple(
            ExecutionRowAttempt(
                row_id=f"row-{index}",
                dataset="customers",
                source_row=index,
                target_model="res.partner",
                operation=("CREATE" if index == 1 else "UPDATE"),
                field_names=("name",),
                proposed_external_id=("customer-1" if index == 1 else ""),
                status=ExecutionRowStatus.COMMITTED,
                attempt=1,
                odoo_id=10 + index,
            )
            for index in (1, 2)
        )
        self.execution = ExecutionRun(
            run_id=str(uuid4()),
            project_id=self.project_id,
            snapshot_hash=self.snapshot.semantic_hash,
            snapshot_root_hash=_hash("3"),
            preflight_run_id=self.report.run_id,
            target_hash=self.target.connection_target_hash,
            target_database=self.target.database,
            batch_rows=10,
            status=ExecutionRunStatus.COMPLETED,
            started_at=now,
            started_by=LOCAL_ACTOR.identity.display_name,
            completed_at=now,
            rows=rows,
            write_credential_binding_hash=_hash("4"),
            write_principal_hash=_hash("5"),
            write_permission_hash=_hash("6"),
            write_context_hash=_hash("7"),
        )
        reconciliation_rows = tuple(
            ReconciliationRow(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.operation,
                execution_status=row.status.value,
                status=ReconciliationRowStatus.VERIFIED,
                odoo_id=row.odoo_id,
            )
            for row in rows
        )
        self.reconciliation = ReconciliationRun(
            reconciliation_id=str(uuid4()),
            project_id=self.project_id,
            execution_run_id=self.execution.run_id,
            snapshot_hash=self.snapshot.semantic_hash,
            target_hash=self.target.connection_target_hash,
            target_database=self.target.database,
            status=ReconciliationRunStatus.VERIFIED,
            verified_at=now,
            verified_by=LOCAL_ACTOR.identity.display_name,
            unchanged_count=1,
            rows=reconciliation_rows,
        )
        self.recipes = _Recipes(
            self.recipe,
            self.data_version,
            self.semantic_hash,
        )
        self.application_state = _ApplicationState(
            self.draft,
            self.application,
            self.target,
        )
        self.recipe_applications = _RecipeApplications(self.project, self.target)
        self.service = RecipeQualificationService(
            recipes=self.recipes,
            recipe_applications=self.recipe_applications,
            applications=self.application_state,
            mappings=SimpleNamespace(
                get_mapping_revision=lambda _project_id, version=None: SimpleNamespace(
                    mapping_id=self.mapping_id,
                    version=1,
                    definition=SimpleNamespace(content_hash=self.mapping_hash),
                ),
                get_mapping_submission=lambda _project_id, version=None: (
                    SimpleNamespace(
                        mapping_id=self.mapping_id,
                        mapping_content_hash=self.mapping_hash,
                    )
                ),
            ),
            staging=SimpleNamespace(
                get_current_staging_summary=lambda _project_id: self.staging
            ),
            quality=SimpleNamespace(
                get_current_quality_summary=lambda _project_id: self.quality
            ),
            preflight=SimpleNamespace(
                current_report=lambda _project_id: self.report,
                current_execution_snapshot=lambda _project_id: self.snapshot,
            ),
            execution=SimpleNamespace(
                get_current_run=lambda _project_id, snapshot_hash=None: (
                    self.execution
                    if snapshot_hash == self.execution.snapshot_hash
                    else None
                )
            ),
            reconciliation=SimpleNamespace(
                current=lambda _project_id: self.reconciliation
            ),
            authorization=CapabilityAuthorizationPolicy(),
        )

    def _review(self, credential_generation: str | None = None):
        return self.service.review(
            self.recipe_id,
            credential_generation=(
                credential_generation or self.target.credential_generation
            ),
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )

    def test_exact_successful_rehearsal_is_ready_and_can_be_selected(self) -> None:
        review = self._review()

        self.assertEqual(review.state, RecipeQualificationState.READY)
        self.assertTrue(review.can_qualify)
        self.assertEqual(review.expected_outcomes.total_count, 3)
        qualification = self.service.qualify(
            self.recipe_id,
            expected_recipe_revision=7,
            expected_outcomes=review.expected_outcomes.to_dict(),
            credential_generation=self.target.credential_generation,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(qualification.recipe_revision, 3)
        self.assertNotIn("odoo_id", json.dumps(self.recipes.published_evidence))
        self.assertEqual(
            self._review().state,
            RecipeQualificationState.QUALIFIED,
        )

        candidate = self.service.select_current(
            self.recipe_id,
            expected_recipe_revision=8,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(candidate.qualification_id, qualification.qualification_id)
        self.assertEqual(self._review().state, RecipeQualificationState.SELECTED)

    def test_changed_test_read_key_blocks_qualification(self) -> None:
        review = self._review(_hash("8"))

        self.assertEqual(review.state, RecipeQualificationState.REVIEW_REQUIRED)
        self.assertFalse(review.can_qualify)
        self.assertIn("TARGET_BINDING_STALE", {item.code for item in review.issues})

    def test_outcome_confirmation_is_optimistic_and_exact(self) -> None:
        review = self._review()
        changed = review.expected_outcomes.to_dict()
        changed["verified_count"] += 1

        with self.assertRaises(RecipeQualificationError):
            self.service.qualify(
                self.recipe_id,
                expected_recipe_revision=7,
                expected_outcomes=changed,
                credential_generation=self.target.credential_generation,
                credential_storage_class="SESSION",
                actor=LOCAL_ACTOR,
            )

    def test_later_recipe_revision_is_untested_and_does_not_inherit_v3(self) -> None:
        review = self._review()
        self.service.qualify(
            self.recipe_id,
            expected_recipe_revision=7,
            expected_outcomes=review.expected_outcomes.to_dict(),
            credential_generation=self.target.credential_generation,
            credential_storage_class="SESSION",
            actor=LOCAL_ACTOR,
        )
        self.recipes.recipe = replace(
            self.recipes.recipe,
            current_recipe_revision=4,
            optimistic_revision=9,
        )

        later = self._review()

        self.assertEqual(later.state, RecipeQualificationState.UNTESTED)
        self.assertIsNone(later.qualification)
        self.assertEqual(later.issues[0].code, "RECIPE_REVISION_UNTESTED")


if __name__ == "__main__":
    unittest.main()
