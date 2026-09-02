"""Expose the focused completed-load correction use cases to the browser.

This facade joins existing evidence owners once. It does not classify source
rows, search Odoo by business key, or expose protected identifiers and values
to browser projections.
"""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.correction_execution import (
    CorrectionExecutionResult,
    CorrectionExecutionService,
)
from impodo.application.correction_orchestration import (
    CorrectionBinding,
    CorrectionBindingRepository,
    CorrectionOriginPublication,
    CorrectionOriginPublisher,
    CorrectionOriginRequest,
    CorrectionProtectedStore,
    CorrectionReviewOrchestrator,
    CorrectionSuccessor,
    CorrectionSuccessorService,
)
from impodo.application.correction_service import CorrectionReview
from impodo.domain.correction import (
    CorrectionConfirmation,
    CorrectionPlan,
    CorrectionPlanSummary,
)
from impodo.domain.correction_origin import (
    CorrectionOriginError,
)
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import OdooWriteIdentity


@dataclass(frozen=True, slots=True)
class CorrectionJourneyView:
    """Safe browser projection without IDs from Odoo, values, or hashes."""

    project_id: str
    completed_workspace_id: str
    successor_workspace_id: str | None
    has_current_plan: bool
    has_confirmation: bool
    completed: bool
    plan_summary: CorrectionPlanSummary | None


@dataclass(frozen=True, slots=True)
class _StoredTargetIndexReference:
    project_id: str
    index_id: str
    index_hash: str
    storage_key: str
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class _StoredOriginReference:
    project_id: str
    manifest_id: str
    manifest_hash: str
    storage_key: str
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class _StoredPlanReference:
    project_id: str
    plan_id: str
    plan_hash: str
    storage_key: str
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class _StoredConfirmationReference:
    project_id: str
    confirmation_id: str
    confirmation_hash: str
    storage_key: str
    artifact_hash: str


class CorrectionWorkflowService:
    """Coordinate publication, review, confirmation, and verified apply."""

    def __init__(
        self,
        *,
        bindings: CorrectionBindingRepository,
        protected: CorrectionProtectedStore,
        origin_publisher: CorrectionOriginPublisher,
        successors: CorrectionSuccessorService,
        reviewer: CorrectionReviewOrchestrator,
        executor: CorrectionExecutionService,
        runs,
        workspaces,
        mappings,
        preparations,
        preflight,
        preflight_repository,
        executions,
        reconciliations,
    ) -> None:
        self.bindings = bindings
        self.protected = protected
        self.origin_publisher = origin_publisher
        self.successors = successors
        self.reviewer = reviewer
        self.executor = executor
        self.runs = runs
        self.workspaces = workspaces
        self.mappings = mappings
        self.preparations = preparations
        self.preflight = preflight
        self.preflight_repository = preflight_repository
        self.executions = executions
        self.reconciliations = reconciliations

    def publish_completed_load(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionOriginPublication:
        """Seal one verified Authoring load from its current exact evidence."""

        current = self.bindings.get_for_completed_workspace(workspace_id)
        if current is not None:
            return CorrectionOriginPublication(
                current,
                self._origin(current),
                self._target_index(current),
            )
        workspace = self.workspaces.get(workspace_id, actor=actor)
        run = self.runs.get(workspace.migration_run_id, actor=actor)
        mapping = self.mappings.get_mapping_revision(workspace_id)
        prepared = self.preparations.current_prepared_snapshots(workspace_id)
        snapshot = self.preflight.current_execution_snapshot(workspace_id)
        execution = self.executions.get_current_run(workspace_id)
        reconciliation = self.reconciliations.get_current(workspace_id)
        if (
            mapping is None
            or snapshot is None
            or execution is None
            or reconciliation is None
        ):
            raise CorrectionOriginError(
                "Verified completed-load evidence is incomplete",
                failure_code="CORRECTION_ORIGIN_COMPLETED_EVIDENCE_INCOMPLETE",
            )
        records = self.preflight_repository.get_record_snapshot(
            workspace_id,
            snapshot.preflight_run_id,
        )
        if records is None:
            raise CorrectionOriginError(
                "Completed-load target evidence is missing",
                failure_code="CORRECTION_ORIGIN_TARGET_EVIDENCE_MISSING",
            )
        return self.origin_publisher.publish(
            CorrectionOriginRequest(
                completed_run=run,
                completed_workspace=workspace,
                mapping=mapping,
                prepared_snapshots=prepared,
                execution_snapshot=snapshot,
                execution=execution,
                reconciliation=reconciliation,
                target_records=records,
            ),
            actor=actor,
        )

    def get(
        self,
        completed_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionJourneyView | None:
        binding = self.bindings.get_for_completed_workspace(completed_workspace_id)
        return self._view(binding, actor=actor) if binding is not None else None

    def list_for_project(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[CorrectionJourneyView, ...]:
        return tuple(
            self._view(binding, actor=actor)
            for binding in self.bindings.list_for_project(project_id)
        )

    def binding_for_successor(
        self,
        successor_workspace_id: str,
    ) -> CorrectionBinding | None:
        return self.bindings.get_for_successor_workspace(successor_workspace_id)

    def start(
        self,
        completed_workspace_id: str,
        *,
        actor: Actor,
        request_id: str,
    ) -> CorrectionSuccessor:
        return self.successors.start(
            completed_workspace_id,
            actor=actor,
            request_id=request_id,
        )

    def review(
        self,
        completed_workspace_id: str,
        *,
        actor: Actor,
        review_request_id: str,
    ) -> tuple[CorrectionReview, CorrectionPlan | None, CorrectionBinding]:
        binding = self._binding(completed_workspace_id)
        return self.reviewer.review(
            self._origin(binding),
            self._target_index(binding),
            actor=actor,
            review_request_id=review_request_id,
        )

    def current_plan(self, completed_workspace_id: str) -> CorrectionPlan | None:
        binding = self._binding(completed_workspace_id)
        if binding.current_plan is None:
            return None
        return self.protected.read_plan(self._plan_reference(binding))

    def current_confirmation(
        self,
        completed_workspace_id: str,
    ) -> CorrectionConfirmation | None:
        binding = self._binding(completed_workspace_id)
        if binding.current_confirmation is None:
            return None
        return self.protected.read_confirmation(
            self._confirmation_reference(binding)
        )

    def confirm(
        self,
        completed_workspace_id: str,
        *,
        confirmation_id: str,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        actor: Actor,
    ) -> tuple[CorrectionConfirmation, CorrectionBinding]:
        plan = self.current_plan(completed_workspace_id)
        if plan is None:
            raise CorrectionOriginError("Review the correction before applying it")
        return self.executor.confirm(
            completed_workspace_id,
            plan,
            confirmation_id=confirmation_id,
            write_credential_binding_hash=write_credential_binding_hash,
            write_identity=write_identity,
            actor=actor,
        )

    def execute(
        self,
        completed_workspace_id: str,
        *,
        target_database: str,
        write_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        reader: OdooReadbackReader,
        writer: OdooWriteExecutor,
        actor: Actor,
    ) -> CorrectionExecutionResult:
        plan = self.current_plan(completed_workspace_id)
        confirmation = self.current_confirmation(completed_workspace_id)
        if plan is None or confirmation is None:
            raise CorrectionOriginError("Correction confirmation is missing")
        return self.executor.execute(
            completed_workspace_id,
            plan,
            confirmation,
            target_database=target_database,
            write_credential_binding_hash=write_credential_binding_hash,
            write_identity=write_identity,
            reader=reader,
            writer=writer,
            actor=actor,
        )

    def _view(
        self,
        binding: CorrectionBinding,
        *,
        actor: Actor,
    ) -> CorrectionJourneyView:
        plan = (
            self.protected.read_plan(self._plan_reference(binding))
            if binding.current_plan is not None
            else None
        )
        completed = False
        if binding.successor_workspace_id is not None:
            successor = self.workspaces.get(
                binding.successor_workspace_id,
                actor=actor,
            )
            completed = successor.state.value == "CLOSED"
        return CorrectionJourneyView(
            project_id=binding.project_id,
            completed_workspace_id=binding.completed_workspace_id,
            successor_workspace_id=binding.successor_workspace_id,
            has_current_plan=plan is not None,
            has_confirmation=binding.current_confirmation is not None,
            completed=completed,
            plan_summary=plan.public_summary() if plan is not None else None,
        )

    def _binding(self, completed_workspace_id: str) -> CorrectionBinding:
        binding = self.bindings.get_for_completed_workspace(completed_workspace_id)
        if binding is None:
            raise CorrectionOriginError("Completed load is not eligible for correction")
        return binding

    def _origin(self, binding: CorrectionBinding):
        reference = binding.origin
        return self.protected.read_origin(
            _StoredOriginReference(
                binding.project_id,
                reference.artifact_id,
                reference.logical_hash,
                reference.storage_key,
                reference.artifact_hash,
            )
        )

    def _target_index(self, binding: CorrectionBinding):
        reference = binding.target_index
        return self.protected.read_target_index(
            _StoredTargetIndexReference(
                binding.project_id,
                reference.artifact_id,
                reference.logical_hash,
                reference.storage_key,
                reference.artifact_hash,
            )
        )

    @staticmethod
    def _plan_reference(binding: CorrectionBinding) -> _StoredPlanReference:
        reference = binding.current_plan
        if reference is None:
            raise CorrectionOriginError("Correction plan is missing")
        return _StoredPlanReference(
            binding.project_id,
            reference.artifact_id,
            reference.logical_hash,
            reference.storage_key,
            reference.artifact_hash,
        )

    @staticmethod
    def _confirmation_reference(
        binding: CorrectionBinding,
    ) -> _StoredConfirmationReference:
        reference = binding.current_confirmation
        if reference is None:
            raise CorrectionOriginError("Correction confirmation is missing")
        return _StoredConfirmationReference(
            binding.project_id,
            reference.artifact_id,
            reference.logical_hash,
            reference.storage_key,
            reference.artifact_hash,
        )


__all__ = ["CorrectionJourneyView", "CorrectionWorkflowService"]
