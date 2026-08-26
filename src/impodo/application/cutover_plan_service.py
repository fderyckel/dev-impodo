"""Review and publish exact Project-level integrated Test qualification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.data_version.models import DataVersionPurpose, DataVersionState
from ..domain.execution import ExecutionRunStatus
from ..domain.errors import ReadinessError
from ..domain.reconciliation import ReconciliationRunStatus
from ..domain.serialization import content_hash
from ..domain.cutover.models import (
    ApplicationQualificationEvidence,
    CutoverPlanQualification,
    CutoverPlanRevision,
    CutoverQualificationIssue,
    CutoverQualificationState,
    ProjectCutoverSelection,
    QualifiedOutcomes,
    integrated_evidence_payload,
)
from ..migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    require_hash,
    require_revision,
    require_uuid,
)
from ..domain.run.models import MigrationRunPurpose
from ..migration_run_planning import RunRecipeApplication


class IntegratedQualificationEvidenceReader(Protocol):
    def read(
        self,
        application: RunRecipeApplication,
        *,
        target_binding_hash: str,
    ) -> tuple[
        ApplicationQualificationEvidence | None,
        tuple[CutoverQualificationIssue, ...],
    ]: ...

    def reconciliation_current(self, workspace_id: str): ...


@dataclass(frozen=True, slots=True)
class IntegratedQualificationReview:
    project_id: str
    migration_run_id: str
    plan: CutoverPlanRevision
    state: CutoverQualificationState
    applications: tuple[RunRecipeApplication, ...]
    evidence: tuple[ApplicationQualificationEvidence, ...]
    issues: tuple[CutoverQualificationIssue, ...]
    integrated_payload: Mapping[str, object] | None
    integrated_evidence_hash: str | None
    qualification: CutoverPlanQualification | None
    selection: ProjectCutoverSelection | None

    @property
    def can_qualify(self) -> bool:
        return (
            self.state is CutoverQualificationState.READY
            and not self.issues
            and self.integrated_payload is not None
            and self.integrated_evidence_hash is not None
        )

    @property
    def can_select(self) -> bool:
        return (
            self.state is CutoverQualificationState.QUALIFIED
            and self.qualification is not None
        )


class WorkspaceIntegratedQualificationEvidenceReader:
    """Read each selected workspace once without contacting Odoo."""

    def __init__(
        self,
        *,
        mappings,
        staging,
        quality,
        preflight,
        execution,
        reconciliation,
    ) -> None:
        self.mappings = mappings
        self.staging = staging
        self.quality = quality
        self.preflight = preflight
        self.execution = execution
        self.reconciliation = reconciliation

    def reconciliation_current(self, workspace_id: str):
        return self.reconciliation.current(workspace_id)

    def read(
        self,
        application: RunRecipeApplication,
        *,
        target_binding_hash: str,
    ) -> tuple[
        ApplicationQualificationEvidence | None,
        tuple[CutoverQualificationIssue, ...],
    ]:
        workspace_id = application.workspace_id
        issues: list[CutoverQualificationIssue] = []
        revision = self.mappings.get_mapping_revision(workspace_id)
        submission = (
            self.mappings.get_mapping_submission(workspace_id, revision.version)
            if revision is not None
            else None
        )
        if (
            revision is None
            or submission is None
            or application.mapping_id != revision.mapping_id
            or application.mapping_content_hash != revision.definition.content_hash
            or submission.mapping_id != revision.mapping_id
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            issues.append(
                self._issue(
                    "TEST_MATCHING_NOT_CURRENT",
                    "The submitted field matches are not the exact Recipe matches.",
                    "Open this application and submit the current field matches.",
                    workspace_id,
                )
            )

        staging = self.staging.get_current_staging_summary(workspace_id)
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
                    "The Test data is not fully prepared from the submitted matches.",
                    "Prepare this application again and resolve blocked rows.",
                    workspace_id,
                )
            )
        elif not staging.control_totals_passed:
            issues.append(
                self._issue(
                    "TEST_CONTROLS_FAILED",
                    "One or more application control totals did not reconcile.",
                    "Correct the data or expected totals, then prepare again.",
                    workspace_id,
                )
            )

        quality = self.quality.get_current_quality_summary(workspace_id)
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
                    "The prepared data checks are not fully resolved.",
                    "Resolve every data check before comparing with Odoo.",
                    workspace_id,
                )
            )

        report = self.preflight.current_report(workspace_id)
        if report is None or report.status != "READY" or report.attention_count:
            issues.append(
                self._issue(
                    "TEST_COMPARISON_INCOMPLETE",
                    "The current Odoo comparison is not ready for an exact load.",
                    "Compare again and resolve every item needing attention.",
                    workspace_id,
                )
            )
        try:
            snapshot = self.preflight.current_execution_snapshot(workspace_id)
        except ReadinessError:
            snapshot = None
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
                    workspace_id,
                )
            )

        run = (
            self.execution.get_current_run(workspace_id, snapshot.semantic_hash)
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
            or run.completed_at is None
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
                    "Complete a successful Test load for this application.",
                    workspace_id,
                )
            )

        reconciliation = self.reconciliation.current(workspace_id)
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
                    "Read the result back and resolve every difference.",
                    workspace_id,
                )
            )

        if issues or any(
            item is None
            for item in (
                revision,
                staging,
                quality,
                report,
                snapshot,
                run,
                reconciliation,
            )
        ):
            return None, self._deduplicate(issues)
        assert run is not None and run.completed_at is not None
        assert staging is not None and quality is not None
        assert report is not None and reconciliation is not None
        outcomes = QualifiedOutcomes(
            create_count=report.create_count,
            update_count=report.update_count,
            unchanged_count=report.unchanged_count,
            verified_count=reconciliation.verified_count,
        )
        execution_hash = content_hash(
            {
                "completed_at": run.completed_at,
                "preflight_run_id": run.preflight_run_id,
                "rows": [json.loads(item.to_json()) for item in run.rows],
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
        control_hash = content_hash(
            {
                "control_totals": [
                    item.to_portable_dict() for item in staging.control_totals
                ],
                "passed": staging.control_totals_passed,
            }
        )
        unhashed = {
            "application_id": application.application_id,
            "comparison_hash": content_hash(json.loads(report.to_json())),
            "contract_version": 1,
            "control_hash": control_hash,
            "execution_completed_at": run.completed_at.isoformat(),
            "execution_hash": execution_hash,
            "execution_started_at": run.started_at.isoformat(),
            "mapping_content_hash": application.mapping_content_hash,
            "migration_run_id": application.migration_run_id,
            "outcomes": outcomes.to_dict(),
            "preparation_hash": staging.content_hash,
            "project_id": application.project_id,
            "quality_hash": quality.content_hash,
            "read_back_hash": reconciliation.semantic_hash,
            "recipe_id": application.recipe_id,
            "recipe_revision": application.recipe_revision,
            "recipe_semantic_hash": application.recipe_semantic_hash,
            "reconciled_at": reconciliation.verified_at.isoformat(),
            "reconciliation_hash": content_hash(
                reconciliation.portable_dict(include_hash=False)
            ),
            "target_binding_hash": target_binding_hash,
            "workspace_id": workspace_id,
        }
        return (
            ApplicationQualificationEvidence(
                application_id=application.application_id,
                project_id=application.project_id,
                migration_run_id=application.migration_run_id,
                workspace_id=workspace_id,
                recipe_id=application.recipe_id,
                recipe_revision=application.recipe_revision,
                recipe_semantic_hash=application.recipe_semantic_hash,
                target_binding_hash=target_binding_hash,
                mapping_content_hash=str(application.mapping_content_hash),
                preparation_hash=staging.content_hash,
                quality_hash=quality.content_hash,
                comparison_hash=str(unhashed["comparison_hash"]),
                execution_hash=execution_hash,
                read_back_hash=reconciliation.semantic_hash,
                reconciliation_hash=str(unhashed["reconciliation_hash"]),
                control_hash=control_hash,
                outcomes=outcomes,
                execution_started_at=run.started_at,
                execution_completed_at=run.completed_at,
                reconciled_at=reconciliation.verified_at,
                content_hash=content_hash(unhashed),
            ),
            (),
        )

    @staticmethod
    def _issue(
        code: str,
        message: str,
        recovery_action: str,
        workspace_id: str,
    ) -> CutoverQualificationIssue:
        return CutoverQualificationIssue(
            code=code,
            message=message,
            recovery_action=recovery_action,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _deduplicate(
        issues: list[CutoverQualificationIssue],
    ) -> tuple[CutoverQualificationIssue, ...]:
        return tuple({item.code: item for item in issues}.values())


class CutoverPlanService:
    """Own exact Project qualification and rollout-candidate selection."""

    def __init__(
        self,
        *,
        projects,
        data_versions,
        run_planning,
        repository,
        evidence_reader: IntegratedQualificationEvidenceReader,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.run_planning = run_planning
        self.repository = repository
        self.evidence_reader = evidence_reader
        self.authorization = authorization

    def review(
        self,
        project_id: str,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> IntegratedQualificationReview:
        project_id = require_uuid(project_id, "project_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        self.projects.get(project_id, actor=actor)
        bundle = self.run_planning.get_bundle(migration_run_id)
        if (
            bundle.run.project_id != project_id
            or bundle.run.purpose is not MigrationRunPurpose.TEST
        ):
            raise MigrationConflictError(
                "Integrated qualification requires this Project's Test run"
            )
        binding = self.repository.get_run_binding(migration_run_id)
        plan = self.repository.get_revision(
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
        )
        if binding.plan_content_hash != plan.content_hash:
            raise MigrationConflictError("Test run CutoverPlan binding is inconsistent")
        by_recipe = {item.recipe_id: item for item in bundle.applications}
        try:
            ordered_applications = tuple(
                by_recipe[recipe_id]
                for recipe_id in bundle.requirement_plan.application_order
            )
        except KeyError as error:
            raise MigrationConflictError(
                "Test run applications do not match their required order"
            ) from error
        qualifications = self.repository.list_qualifications(
            plan.cutover_plan_id,
            plan.version,
        )
        selection = self.repository.current_selection(project_id)
        if qualifications:
            qualification = qualifications[0]
            selected = (
                selection
                if selection is not None
                and selection.qualification_id == qualification.qualification_id
                else None
            )
            return IntegratedQualificationReview(
                project_id=project_id,
                migration_run_id=migration_run_id,
                plan=plan,
                state=(
                    CutoverQualificationState.SELECTED
                    if selected is not None
                    else CutoverQualificationState.QUALIFIED
                ),
                applications=ordered_applications,
                evidence=(),
                issues=(),
                integrated_payload=None,
                integrated_evidence_hash=qualification.integrated_evidence_hash,
                qualification=qualification,
                selection=selected,
            )

        issues: list[CutoverQualificationIssue] = []
        data_version = self.data_versions.get(bundle.run.data_version_id, actor=actor)
        package_complete = (
            data_version.project_id == project_id
            and data_version.purpose is DataVersionPurpose.TEST
            and data_version.state is DataVersionState.FROZEN
            and bool(data_version.source_package_hash)
        )
        if not package_complete:
            issues.append(
                CutoverQualificationIssue(
                    code="PROJECT_TEST_PACKAGE_INCOMPLETE",
                    message="The integrated Test source package is not frozen and complete.",
                    recovery_action="Accept a complete Test DataVersion and start a new run.",
                )
            )
        evidence = []
        for application in ordered_applications:
            item, application_issues = self.evidence_reader.read(
                application,
                target_binding_hash=bundle.target_binding.content_hash,
            )
            issues.extend(application_issues)
            if item is not None:
                evidence.append(item)
        by_recipe = {item.recipe_id: item for item in evidence}
        for dependency in plan.dependencies:
            before = by_recipe.get(dependency.before_recipe_id)
            after = by_recipe.get(dependency.after_recipe_id)
            if (
                before is not None
                and after is not None
                and before.reconciled_at > after.execution_started_at
            ):
                workspace_id = next(
                    item.workspace_id
                    for item in ordered_applications
                    if item.recipe_id == dependency.after_recipe_id
                )
                issues.append(
                    CutoverQualificationIssue(
                        code="PROJECT_DEPENDENCY_ORDER_NOT_PROVEN",
                        message=(
                            "A downstream application started before its upstream "
                            "application was verified."
                        ),
                        recovery_action=(
                            "Repeat the downstream Test load after the upstream "
                            "application is fully reconciled."
                        ),
                        workspace_id=workspace_id,
                    )
                )
        evidence_tuple = tuple(sorted(evidence, key=lambda item: item.application_id))
        integrated_reconciliation = (
            len(evidence_tuple) == len(ordered_applications) and not issues
        )
        if issues:
            return IntegratedQualificationReview(
                project_id=project_id,
                migration_run_id=migration_run_id,
                plan=plan,
                state=CutoverQualificationState.NOT_READY,
                applications=ordered_applications,
                evidence=evidence_tuple,
                issues=tuple(
                    sorted(
                        {
                            (item.code, item.workspace_id): item
                            for item in issues
                        }.values(),
                        key=lambda item: (item.code, item.workspace_id or ""),
                    )
                ),
                integrated_payload=None,
                integrated_evidence_hash=None,
                qualification=None,
                selection=None,
            )
        payload = integrated_evidence_payload(
            plan=plan,
            run_id=migration_run_id,
            target_binding_hash=bundle.target_binding.content_hash,
            applications=evidence_tuple,
            shared_controls={
                "control:project.integrated_reconciliation": (
                    integrated_reconciliation
                ),
                "control:project.package_completeness": package_complete,
            },
        )
        return IntegratedQualificationReview(
            project_id=project_id,
            migration_run_id=migration_run_id,
            plan=plan,
            state=CutoverQualificationState.READY,
            applications=ordered_applications,
            evidence=evidence_tuple,
            issues=(),
            integrated_payload=payload,
            integrated_evidence_hash=content_hash(payload),
            qualification=None,
            selection=None,
        )

    def qualify(
        self,
        project_id: str,
        migration_run_id: str,
        *,
        expected_workspace_revision: int,
        expected_evidence_hash: str,
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CutoverPlanQualification:
        project_id = require_uuid(project_id, "project_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        operation_id = require_uuid(operation_id, "operation_id")
        self.authorization.require(
            actor,
            Capability.RECIPE_QUALIFY,
            project_id=project_id,
        )
        self.authorization.require(
            actor,
            Capability.CUTOVER_PLAN_QUALIFY,
            project_id=project_id,
        )
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        expected_evidence_hash = require_hash(
            expected_evidence_hash,
            "expected_evidence_hash",
        )
        committed = self.repository.committed_qualification_for_operation(
            operation_id,
            project_id=project_id,
            migration_run_id=migration_run_id,
            integrated_evidence_hash=expected_evidence_hash,
            expected_workspace_revision=expected_workspace_revision,
            actor=actor,
        )
        if committed is not None:
            return committed
        review = self.review(project_id, migration_run_id, actor=actor)
        project = self.projects.get(project_id, actor=actor)
        if (
            not review.can_qualify
            or review.integrated_evidence_hash != expected_evidence_hash
            or project.optimistic_revision != expected_workspace_revision
        ):
            raise MigrationConflictError(
                "The integrated Test evidence changed; review it again"
            )
        return self.repository.qualify(
            plan=review.plan,
            migration_run_id=migration_run_id,
            application_evidence=review.evidence,
            target_binding_hash=str(
                review.integrated_payload["target_binding_hash"]
            ),
            integrated_payload=dict(review.integrated_payload),
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            actor=actor,
            fault=fault,
        )

    def select(
        self,
        project_id: str,
        qualification_id: str,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        actor: Actor,
    ) -> ProjectCutoverSelection:
        project_id = require_uuid(project_id, "project_id")
        qualification_id = require_uuid(qualification_id, "qualification_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        self.authorization.require(
            actor,
            Capability.CUTOVER_SELECT,
            project_id=project_id,
        )
        committed = self.repository.committed_selection_for_operation(
            operation_id,
            project_id=project_id,
            qualification_id=qualification_id,
            expected_workspace_revision=expected_workspace_revision,
            actor=actor,
        )
        if committed is not None:
            return committed
        qualification = self.repository.get_qualification(qualification_id)
        if qualification.project_id != project_id:
            raise MigrationNotFoundError("CutoverPlan qualification not found")
        project = self.projects.get(project_id, actor=actor)
        if project.optimistic_revision != expected_workspace_revision:
            raise MigrationConflictError("Project changed; reload and retry")
        return self.repository.select(
            qualification_id,
            expected_workspace_revision=expected_workspace_revision,
            operation_id=operation_id,
            actor=actor,
        )

    def assert_application_can_execute(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> None:
        """Fail before any downstream Odoo write when predecessors are unverified."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        workspace = self.run_planning.foundation.get_migration_workspace(workspace_id)
        if workspace.recipe_application_id is None:
            return
        application = self.run_planning.get_application(
            workspace.recipe_application_id
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=application.project_id,
        )
        binding = self.repository.get_run_binding(application.migration_run_id)
        plan = self.repository.get_revision(
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
        )
        predecessors = {
            item.before_recipe_id
            for item in plan.dependencies
            if item.after_recipe_id == application.recipe_id
        }
        if not predecessors:
            return
        by_recipe = {
            item.recipe_id: item
            for item in self.run_planning.list_applications(
                application.migration_run_id
            )
        }
        for recipe_id in sorted(predecessors):
            predecessor = by_recipe.get(recipe_id)
            if predecessor is None:
                raise MigrationConflictError(
                    "CutoverPlan predecessor application is missing"
                )
            reconciliation = self.evidence_reader.reconciliation_current(
                predecessor.workspace_id
            )
            if (
                reconciliation is None
                or reconciliation.status is not ReconciliationRunStatus.VERIFIED
                or reconciliation.fallout_count
                or reconciliation.unknown_count
            ):
                raise MigrationConflictError(
                    "Finish and verify the upstream Recipe application before "
                    "loading this one."
                )
