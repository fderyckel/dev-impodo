"""Qualify one exact Recipe revision from a complete remote Test rehearsal."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.execution import ExecutionRun, ExecutionRunStatus
from ..domain.mapping.artifacts import MappingRevision, MappingSubmission
from ..domain.recipe_applications import (
    RecipeApplicationDraft,
    RecipeApplicationEvidence,
    RecipeApplicationState,
    TargetBinding,
    TargetCredentialRole,
    TargetEnvironment,
)
from ..domain.recipe_qualifications import (
    CutoverCandidateRecord,
    QualificationExpectedOutcomes,
    RecipeQualificationError,
    RecipeQualificationIssue,
    RecipeQualificationRecord,
    RecipeQualificationState,
)
from ..domain.reconciliation import ReconciliationRun, ReconciliationRunStatus
from ..domain.serialization import content_hash
from ..projects import WorkspaceState
from ..quality import QualityRunSummary
from ..recipes import DataVersion, DataVersionPurpose, Recipe
from ..staging import StagingRunSummary
from .preflight_service import PreflightService
from .recipe_application_service import RecipeApplicationService
from .recipe_service import RecipeService
from .reconciliation_service import ReconciliationService


class QualificationApplicationRepository(Protocol):
    def get_draft(self, project_id: str) -> RecipeApplicationDraft | None: ...
    def get_evidence(
        self,
        project_id: str,
        application_id: str,
    ) -> RecipeApplicationEvidence | None: ...
    def get_target_binding(self, project_id: str) -> TargetBinding | None: ...


class QualificationMappingRepository(Protocol):
    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None: ...
    def get_mapping_submission(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None: ...


class QualificationStagingRepository(Protocol):
    def get_current_staging_summary(
        self,
        project_id: str,
    ) -> StagingRunSummary | None: ...


class QualificationQualityRepository(Protocol):
    def get_current_quality_summary(
        self,
        project_id: str,
    ) -> QualityRunSummary | None: ...


class QualificationExecutionRepository(Protocol):
    def get_current_run(
        self,
        project_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None: ...


@dataclass(frozen=True, slots=True)
class RecipeQualificationReview:
    """Data-manager view of current Test evidence and the next explicit action."""

    recipe: Recipe
    data_version: DataVersion | None
    project: WorkspaceState | None
    state: RecipeQualificationState
    issues: tuple[RecipeQualificationIssue, ...]
    expected_outcomes: QualificationExpectedOutcomes | None
    qualification: RecipeQualificationRecord | None
    cutover_candidate: CutoverCandidateRecord | None
    evidence: Mapping[str, object] | None = None

    @property
    def can_qualify(self) -> bool:
        return (
            self.state is RecipeQualificationState.READY
            and self.expected_outcomes is not None
            and self.evidence is not None
            and not self.issues
        )

    @property
    def can_select(self) -> bool:
        return (
            self.state is RecipeQualificationState.QUALIFIED
            and self.qualification is not None
        )


class RecipeQualificationService:
    """Derive and publish qualification only from exact current Test evidence."""

    def __init__(
        self,
        *,
        recipes: RecipeService,
        recipe_applications: RecipeApplicationService,
        applications: QualificationApplicationRepository,
        mappings: QualificationMappingRepository,
        staging: QualificationStagingRepository,
        quality: QualificationQualityRepository,
        preflight: PreflightService,
        execution: QualificationExecutionRepository,
        reconciliation: ReconciliationService,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.recipes = recipes
        self.recipe_applications = recipe_applications
        self.applications = applications
        self.mappings = mappings
        self.staging = staging
        self.quality = quality
        self.preflight = preflight
        self.execution = execution
        self.reconciliation = reconciliation
        self.authorization = authorization

    def review(
        self,
        recipe_id: str,
        *,
        credential_generation: str,
        credential_storage_class: str,
        actor: Actor,
    ) -> RecipeQualificationReview:
        """Evaluate the full rehearsal without publishing or changing evidence."""

        self.authorization.require(actor, Capability.RECIPE_VIEW)
        recipe = self.recipes.get(recipe_id, actor=actor)
        versions = self.recipes.data_versions(recipe_id, actor=actor)
        data_version = next(
            (
                item
                for item in versions
                if item.data_version_id == recipe.current_data_version_id
            ),
            None,
        )
        project = (
            self.recipe_applications.project_reader.get(
                data_version.workspace_project_id
            )
            if data_version is not None
            else None
        )
        qualification = self.recipes.current_qualification(
            recipe_id,
            actor=actor,
        )
        candidate = self.recipes.cutover_candidate(recipe_id, actor=actor)
        if qualification is not None:
            state = (
                RecipeQualificationState.SELECTED
                if candidate is not None
                and candidate.qualification_id == qualification.qualification_id
                else RecipeQualificationState.QUALIFIED
            )
            return RecipeQualificationReview(
                recipe=recipe,
                data_version=data_version,
                project=project,
                state=state,
                issues=(),
                expected_outcomes=None,
                qualification=qualification,
                cutover_candidate=candidate,
            )

        issues: list[RecipeQualificationIssue] = []
        untested = False
        if recipe.current_recipe_revision is None:
            issues.append(
                self._issue(
                    "RECIPE_NOT_PUBLISHED",
                    "Publish the reusable Recipe rules before testing them.",
                    "Complete the current authoring data version.",
                    f"/recipes/{recipe_id}",
                )
            )
            untested = True
        if data_version is None or data_version.purpose is not DataVersionPurpose.TEST:
            issues.append(
                self._issue(
                    "TEST_DATA_VERSION_REQUIRED",
                    "The current Recipe revision has not been run in a Test data version.",
                    "Create a Test data version with representative replacement data.",
                    f"/recipes/{recipe_id}/test",
                )
            )
            untested = True
        elif data_version.pinned_recipe_revision != recipe.current_recipe_revision:
            issues.append(
                self._issue(
                    "RECIPE_REVISION_UNTESTED",
                    f"Recipe v{recipe.current_recipe_revision} has not been tested.",
                    "Create a new Test data version pinned to this revision.",
                    f"/recipes/{recipe_id}/test",
                )
            )
            untested = True
        if issues or project is None or data_version is None:
            return self._review(
                recipe,
                data_version,
                project,
                RecipeQualificationState.UNTESTED
                if untested
                else RecipeQualificationState.REVIEW_REQUIRED,
                issues,
                candidate=candidate,
            )

        application_review = self.recipe_applications.review(
            recipe_id,
            credential_generation=credential_generation,
            credential_storage_class=credential_storage_class,
            actor=actor,
        )
        if (
            not application_review.can_apply
            or application_review.target_binding is None
        ):
            first = next(
                (item for item in application_review.issues if item.blocks),
                None,
            )
            issues.append(
                self._issue(
                    first.code if first else "TARGET_BINDING_STALE",
                    first.message
                    if first
                    else "The Test target binding is not current.",
                    first.recovery_action
                    if first
                    else "Review the Recipe application again.",
                    f"/recipes/{recipe_id}/application",
                )
            )

        draft = self.applications.get_draft(project.project_id)
        if (
            draft is None
            or draft.state is not RecipeApplicationState.APPLIED
            or draft.recipe_revision != recipe.current_recipe_revision
            or draft.data_version_id != data_version.data_version_id
        ):
            issues.append(
                self._issue(
                    "RECIPE_APPLICATION_REQUIRED",
                    "This Recipe revision is not applied to the current Test data version.",
                    "Review and apply the Recipe before rehearsing the load.",
                    f"/recipes/{recipe_id}/application",
                )
            )
            return self._review(
                recipe,
                data_version,
                project,
                RecipeQualificationState.REVIEW_REQUIRED,
                issues,
                candidate=candidate,
            )
        application = self.applications.get_evidence(
            project.project_id,
            draft.application_id,
        )
        target_binding = self.applications.get_target_binding(project.project_id)
        envelope = self.recipes.read_revision(
            recipe_id,
            recipe.current_recipe_revision,
            actor=actor,
        )
        if (
            application is None
            or application.status is not RecipeApplicationState.APPLIED
            or application.recipe_semantic_hash != str(envelope["semantic_hash"])
            or application.recipe_revision != recipe.current_recipe_revision
            or application.data_version_id != data_version.data_version_id
            or application.workspace_project_id != project.project_id
        ):
            issues.append(
                self._issue(
                    "QUALIFICATION_EVIDENCE_MISMATCH",
                    "The applied Recipe evidence no longer matches this Test revision.",
                    "Review and apply the Recipe again.",
                    f"/recipes/{recipe_id}/application",
                )
            )
        if (
            target_binding is None
            or target_binding.environment is not TargetEnvironment.TEST
            or target_binding.credential_role is not TargetCredentialRole.READ
            or application is None
            or target_binding.target_binding_id != application.target_binding_id
            or target_binding.content_hash != application.target_binding_hash
            or application_review.target_binding is None
            or application_review.target_binding.content_hash
            != target_binding.content_hash
        ):
            issues.append(
                self._issue(
                    "TARGET_BINDING_STALE",
                    "The successful rehearsal is not bound to the current remote Test server and read key.",
                    "Review the Recipe application and refresh Odoo evidence.",
                    f"/recipes/{recipe_id}/application",
                )
            )

        revision = self.mappings.get_mapping_revision(project.project_id)
        submission = (
            self.mappings.get_mapping_submission(project.project_id, revision.version)
            if revision is not None
            else None
        )
        if (
            application is None
            or revision is None
            or submission is None
            or application.mapping_id != revision.mapping_id
            or application.mapping_content_hash != revision.definition.content_hash
            or submission.mapping_id != revision.mapping_id
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            issues.append(
                self._issue(
                    "TEST_MATCHING_NOT_CURRENT",
                    "The Test field matches are not the exact applied Recipe matches.",
                    "Review and submit matching again.",
                    f"/projects/{project.project_id}/mapping",
                )
            )

        staging = self.staging.get_current_staging_summary(project.project_id)
        if (
            staging is None
            or revision is None
            or staging.mapping_id != revision.mapping_id
            or staging.mapping_version != revision.version
            or staging.attention_rows
        ):
            issues.append(
                self._issue(
                    "TEST_PREPARATION_INCOMPLETE",
                    "The current Test data is not fully prepared from the applied matches.",
                    "Prepare the Test data again and resolve every blocked row.",
                    f"/projects/{project.project_id}/prepare",
                )
            )
        elif not staging.control_totals_passed:
            issues.append(
                self._issue(
                    "TEST_CONTROLS_FAILED",
                    "One or more declared Test control totals did not reconcile.",
                    "Correct the Test data or expected controls, then prepare it again.",
                    f"/projects/{project.project_id}/prepare",
                )
            )

        quality = self.quality.get_current_quality_summary(project.project_id)
        if (
            staging is None
            or quality is None
            or quality.staging_run_id != staging.run_id
            or quality.staging_content_hash != staging.content_hash
            or not quality.ready_for_package
        ):
            issues.append(
                self._issue(
                    "TEST_QUALITY_INCOMPLETE",
                    "The current Test data checks are not fully resolved.",
                    "Review the prepared data and resolve every check before comparing.",
                    f"/projects/{project.project_id}/prepare",
                )
            )

        report = self.preflight.current_report(project.project_id)
        if report is None or report.status != "READY" or report.attention_count:
            issues.append(
                self._issue(
                    "TEST_COMPARISON_INCOMPLETE",
                    "The current Test comparison is not ready for an exact load.",
                    "Compare with Odoo again and resolve every item needing attention.",
                    f"/projects/{project.project_id}/summary",
                )
            )
        snapshot = self.preflight.current_execution_snapshot(project.project_id)
        if (
            report is None
            or snapshot is None
            or snapshot.preflight_run_id != report.run_id
        ):
            issues.append(
                self._issue(
                    "TEST_EXECUTION_PREVIEW_STALE",
                    "The reviewed Test load preview is no longer current.",
                    "Compare with Odoo again before loading.",
                    f"/projects/{project.project_id}/summary",
                )
            )
        run = (
            self.execution.get_current_run(project.project_id, snapshot.semantic_hash)
            if snapshot is not None
            else None
        )
        write_count = (
            int(snapshot.counts.get("CREATE", 0))
            + int(snapshot.counts.get("UPDATE", 0))
            if snapshot is not None
            else 0
        )
        if (
            run is None
            or run.status is not ExecutionRunStatus.COMPLETED
            or run.total_count != write_count
            or run.committed_count != write_count
            or run.failed_count
            or run.partially_applied_count
            or run.blocked_count
            or run.unknown_count
            or run.planned_count
        ):
            issues.append(
                self._issue(
                    "TEST_EXECUTION_NOT_SUCCESSFUL",
                    "The exact current Test load did not complete successfully.",
                    "Complete and verify a successful Test load.",
                    f"/projects/{project.project_id}/load/outcome",
                )
            )
        reconciliation = self.reconciliation.current(project.project_id)
        if (
            reconciliation is None
            or run is None
            or reconciliation.execution_run_id != run.run_id
            or reconciliation.status is not ReconciliationRunStatus.VERIFIED
            or reconciliation.fallout_count
            or reconciliation.unknown_count
            or report is None
            or reconciliation.verified_count != report.total_count
        ):
            issues.append(
                self._issue(
                    "TEST_RECONCILIATION_NOT_VERIFIED",
                    "The current Test load has not been fully verified in Odoo.",
                    "Read the result back from Odoo and resolve every difference.",
                    f"/projects/{project.project_id}/load/outcome",
                )
            )

        if issues or any(
            item is None
            for item in (
                application,
                target_binding,
                staging,
                quality,
                report,
                snapshot,
                run,
                reconciliation,
            )
        ):
            return self._review(
                recipe,
                data_version,
                project,
                RecipeQualificationState.REVIEW_REQUIRED,
                self._deduplicate(issues),
                candidate=candidate,
            )

        expected = QualificationExpectedOutcomes(
            create_count=report.create_count,
            update_count=report.update_count,
            unchanged_count=report.unchanged_count,
            verified_count=reconciliation.verified_count,
        )
        evidence = self._evidence(
            recipe=recipe,
            data_version=data_version,
            application=application,
            target_binding=target_binding,
            staging=staging,
            quality=quality,
            report=report,
            run=run,
            reconciliation=reconciliation,
            expected=expected,
        )
        return RecipeQualificationReview(
            recipe=recipe,
            data_version=data_version,
            project=project,
            state=RecipeQualificationState.READY,
            issues=(),
            expected_outcomes=expected,
            qualification=None,
            cutover_candidate=candidate,
            evidence=evidence,
        )

    def qualify(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        expected_outcomes: Mapping[str, object],
        credential_generation: str,
        credential_storage_class: str,
        actor: Actor,
    ) -> RecipeQualificationRecord:
        self.authorization.require(actor, Capability.RECIPE_QUALIFY)
        review = self.review(
            recipe_id,
            credential_generation=credential_generation,
            credential_storage_class=credential_storage_class,
            actor=actor,
        )
        supplied = QualificationExpectedOutcomes.from_mapping(expected_outcomes)
        if not review.can_qualify or review.expected_outcomes != supplied:
            raise RecipeQualificationError(
                "The Test evidence or expected outcomes changed; review them again"
            )
        if review.recipe.optimistic_revision != expected_recipe_revision:
            raise RecipeQualificationError(
                "The Recipe changed; reload before qualifying it"
            )
        self.recipes.publish_qualification(
            recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            evidence=dict(review.evidence or {}),
            actor=actor,
        )
        result = self.recipes.current_qualification(recipe_id, actor=actor)
        if result is None:
            raise RecipeQualificationError("Qualification could not be reloaded")
        return result

    def select_current(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        actor: Actor,
    ) -> CutoverCandidateRecord:
        self.authorization.require(actor, Capability.CUTOVER_SELECT)
        recipe = self.recipes.get(recipe_id, actor=actor)
        qualification = self.recipes.current_qualification(recipe_id, actor=actor)
        if (
            qualification is None
            or qualification.recipe_revision != recipe.current_recipe_revision
            or recipe.optimistic_revision != expected_recipe_revision
        ):
            raise RecipeQualificationError(
                "Only the current exactly qualified Recipe revision can be selected"
            )
        self.recipes.select_cutover_candidate(
            recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            recipe_revision=qualification.recipe_revision,
            qualification_id=qualification.qualification_id,
            qualification_evidence_hash=qualification.evidence_hash,
            actor=actor,
        )
        candidate = self.recipes.cutover_candidate(recipe_id, actor=actor)
        if candidate is None:
            raise RecipeQualificationError("Rollout candidate could not be reloaded")
        return candidate

    @staticmethod
    def _evidence(
        *,
        recipe: Recipe,
        data_version: DataVersion,
        application: RecipeApplicationEvidence,
        target_binding: TargetBinding,
        staging: StagingRunSummary,
        quality: QualityRunSummary,
        report,
        run: ExecutionRun,
        reconciliation: ReconciliationRun,
        expected: QualificationExpectedOutcomes,
    ) -> dict[str, object]:
        control_hash = content_hash(
            {
                "application_control_values_hash": application.control_values_hash,
                "control_totals": [
                    item.to_portable_dict() for item in staging.control_totals
                ],
                "passed": staging.control_totals_passed,
            }
        )
        execution_hash = content_hash(
            {
                "completed_at": run.completed_at,
                "preflight_run_id": run.preflight_run_id,
                "rows": [
                    {
                        "attempt": row.attempt,
                        "dataset": row.dataset,
                        "field_names": list(row.field_names),
                        "operation": row.operation,
                        "proposed_external_id": row.proposed_external_id,
                        "row_id": row.row_id,
                        "source_row": row.source_row,
                        "status": row.status.value,
                        "target_model": row.target_model,
                    }
                    for row in run.rows
                ],
                "run_id": run.run_id,
                "snapshot_hash": run.snapshot_hash,
                "snapshot_root_hash": run.snapshot_root_hash,
                "status": run.status.value,
                "target_hash": run.target_hash,
                "write_context_hash": run.write_context_hash,
                "write_credential_binding_hash": run.write_credential_binding_hash,
                "write_permission_hash": run.write_permission_hash,
                "write_principal_hash": run.write_principal_hash,
            }
        )
        reconciliation_hash = content_hash(
            {
                "execution_run_id": reconciliation.execution_run_id,
                "fallout_count": reconciliation.fallout_count,
                "reconciliation_id": reconciliation.reconciliation_id,
                "semantic_hash": reconciliation.semantic_hash,
                "status": reconciliation.status.value,
                "unknown_count": reconciliation.unknown_count,
                "verified_count": reconciliation.verified_count,
            }
        )
        return {
            "application_evidence_hash": application.content_hash,
            "application_id": application.application_id,
            "comparison_hash": content_hash(json.loads(report.to_json())),
            "contract_version": 1,
            "control_hash": control_hash,
            "data_version_id": data_version.data_version_id,
            "environment": "TEST",
            "execution_hash": execution_hash,
            "expected_outcomes": expected.to_dict(),
            "findings": [],
            "preparation_hash": staging.content_hash,
            "quality_hash": quality.content_hash,
            "read_back_hash": reconciliation.semantic_hash,
            "recipe_revision": recipe.current_recipe_revision,
            "recipe_semantic_hash": application.recipe_semantic_hash,
            "reconciliation_hash": reconciliation_hash,
            "repeat_preview_hash": None,
            "status": "TEST_QUALIFIED",
            "test_target_binding_hash": target_binding.content_hash,
            "test_target_binding_id": target_binding.target_binding_id,
            "workspace_project_id": data_version.workspace_project_id,
        }

    @staticmethod
    def _issue(
        code: str,
        message: str,
        recovery_action: str,
        recovery_href: str,
    ) -> RecipeQualificationIssue:
        return RecipeQualificationIssue(code, message, recovery_action, recovery_href)

    @staticmethod
    def _deduplicate(
        issues: list[RecipeQualificationIssue],
    ) -> tuple[RecipeQualificationIssue, ...]:
        return tuple({item.code: item for item in issues}.values())

    @staticmethod
    def _review(
        recipe: Recipe,
        data_version: DataVersion | None,
        project: WorkspaceState | None,
        state: RecipeQualificationState,
        issues: list[RecipeQualificationIssue] | tuple[RecipeQualificationIssue, ...],
        *,
        candidate: CutoverCandidateRecord | None,
    ) -> RecipeQualificationReview:
        return RecipeQualificationReview(
            recipe=recipe,
            data_version=data_version,
            project=project,
            state=state,
            issues=tuple(issues),
            expected_outcomes=None,
            qualification=None,
            cutover_candidate=candidate,
        )
