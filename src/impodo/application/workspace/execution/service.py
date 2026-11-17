"""Orchestrate one confirmed schema-bound load from the current snapshot."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from impodo.application.shared.local_operations import (
    LocalOperation,
    direct_local_operation,
)
from impodo.application.workspace.execution.navigation import (
    ExecutionNavigationState,
    ExecutionReviewProjection,
)
from impodo.domain.execution.batch_policy import validated_create_batch_rows
from impodo.domain.execution.identity import portable_key
from impodo.domain.execution.load_control import (
    LoadControl,
    LoadControlState,
    LoadHalted,
    RejectionPolicy,
)
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_scope import (
    execution_api_scope,
)
from impodo.domain.execution.odoo_write import (
    OdooRequestDeferred,
    OdooWriteExecutor,
)
from impodo.domain.execution.operator_errors import (
    OperatorErrorPageRepository,
    OperatorErrorReceipt,
    OperatorErrorRepository,
)
from impodo.domain.execution.recovery import (
    classify_recovery,
)
from impodo.domain.execution.validation import execution_snapshot_issue
from impodo.domain.execution_snapshot import (
    ExecutionSnapshot,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.reconciliation import (
    ReconciliationRun,
)
from impodo.domain.shared.access import (
    Actor,
    Capability,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    target_identity_hash,
    target_record_binding_hash,
)
from impodo.domain.workspace.business_intent_assessment import (
    BusinessIntentAssessment,
    BusinessIntentAssessmentSummary,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    transfer_destination_workspace,
)

from .business_review import BusinessIntentReview
from .identity_resolution import (
    assert_create_rows_still_absent,
    resolve_identity_crosswalk,
)
from .journal import ExecutionJournalRepository, JournalTransitions
from .load_observation import (
    current_load_observation,
    emit_load_evidence,
    observe_load_context,
    observe_load_journal,
    observe_load_local_phase,
)
from .load_transport import (
    LoadControlRepository,
)
from .preview import (
    ExecutionNavigationPreview,
    ExecutionPreview,
    build_execution_preview,
    planned_deferred_create_count,
)
from .progress import ExecutionProgress, ExecutionProgressPublisher
from .validation_messages import (
    NO_WRITE_ROWS_MESSAGE,
    execution_scope_error_code,
    execution_snapshot_message,
)
from .writer import LoadWriter

DEFAULT_CREATE_BATCH_ROWS = 10
MAX_PROGRESS_ROWS_BETWEEN_UPDATES = 25
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ReadCredentialBindingProvider = Callable[[WorkspaceState, Actor], str]


def _business_intent_error(assessment: BusinessIntentAssessment | BusinessIntentAssessmentSummary | None) -> str:
    """Give one actionable blocker without rendering unbounded rule detail."""
    if assessment is None:
        return "Review business expectations against the current comparison before loading"
    return assessment.deviations[0] if assessment.deviations else "Review business expectations before loading"


class ExecutionWorkspaceRepository(Protocol):
    """Read the target configuration needed to validate an execution snapshot."""

    def get(self, workspace_id: str) -> WorkspaceState:
        """Return state for preview, execute, resume, and completion commands."""
        ...


class ExecutionSnapshotRepository(Protocol):
    def business_intent_review(self, workspace_id: str, snapshot_hash: str, *, actor: Actor, run_id: str = "") -> BusinessIntentReview | None: ...
    def current_business_intent_assessment(self, workspace_id: str, snapshot_hash: str) -> BusinessIntentAssessment | BusinessIntentAssessmentSummary | None: ...

    def require_business_intent_approval(self, workspace_id: str, run: ExecutionRun) -> None: ...

    """Read current and historical immutable load-preview snapshots."""

    def current_execution_snapshot(
        self,
        workspace_id: str,
    ) -> ExecutionSnapshot | None:
        """Return the current approved snapshot, if one remains valid."""
        ...

    def execution_snapshot(
        self,
        workspace_id: str,
        preflight_run_id: str,
    ) -> ExecutionSnapshot:
        """Return the exact snapshot bound to a historical comparison."""
        ...

    def current_execution_review_projection(
        self,
        workspace_id: str,
    ) -> ExecutionReviewProjection | None:
        """Return bounded Check changes facts without snapshot rows."""
        ...


@dataclass(frozen=True, slots=True)
class RecoveryResumePlan:
    """Safe counts from a read-only assessment of one interrupted load."""

    execution_run_id: str
    snapshot_hash: str
    total_count: int
    already_accepted_count: int
    waiting_count: int
    uncertain_count: int
    confirmed_after_interruption_count: int
    safe_to_retry_count: int
    relationship_pending_count: int
    resume_count: int
    load_control_hash: str = ""


@dataclass(frozen=True, slots=True, repr=False)
class _ExecutionAdmission:
    """Bind checked local execution facts without retaining a database owner."""

    workspace_state: WorkspaceState
    preview: ExecutionPreview
    batch_rows: int


@dataclass(frozen=True, slots=True, repr=False)
class _RecoveryAdmission:
    """Carry the exact locally validated journal to a later remote phase."""

    workspace_state: WorkspaceState
    snapshot: ExecutionSnapshot
    run: ExecutionRun


@dataclass(frozen=True, slots=True, repr=False)
class _SettledHalt:
    """Preserve which progress path follows a local stopped-run settlement."""

    run: ExecutionRun
    was_terminal: bool


class ExecutionService:
    """Validate, journal, and execute a reviewed disposable-target load."""

    def __init__(
        self,
        workspaces: ExecutionWorkspaceRepository,
        preflight: ExecutionSnapshotRepository,
        journal: ExecutionJournalRepository,
        authorization: WorkspaceAuthorizationPolicy,
        *,
        require_remote_read_identity: bool = False,
        require_remote_write_identity: bool = False,
        current_read_credential_binding: (
            ReadCredentialBindingProvider | None
        ) = None,
        load_controls: LoadControlRepository | None = None,
        local_operation: LocalOperation | None = None,
    ) -> None:
        self.workspaces = workspaces
        self.preflight = preflight
        self.journal = journal
        self.authorization = authorization
        self.require_remote_read_identity = require_remote_read_identity
        self.require_remote_write_identity = require_remote_write_identity
        self.current_read_credential_binding = current_read_credential_binding
        self.load_controls = load_controls
        self._run_local = local_operation or direct_local_operation

    def _local_execution_check[T](self, function: Callable[[], T]) -> T:
        """Observe one bounded local check without extending its owner lifetime."""

        with observe_load_local_phase("execution_checks"):
            return self._run_local(function)

    def current_preview(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> ExecutionPreview | None:
        """Build full load-review evidence for one workspace.

        Called by load-review routes, transfer execution, resume, and
        no-change completion commands.  It combines the current immutable
        preflight snapshot with current target configuration and journal state,
        but does not authorize or change either one.
        """
        snapshot = self.preflight.current_execution_snapshot(workspace_id)
        if snapshot is None:
            return None
        workspace_state = self.workspaces.get(workspace_id)
        current_read_credential_binding = (
            self.current_read_credential_binding(workspace_state, actor)
            if self.current_read_credential_binding is not None
            else None
        )
        current = self.journal.get_current_run(workspace_id, snapshot.semantic_hash)
        prior = self.journal.get_current_run(workspace_id) if current is None else None
        if prior is not None and prior.status is not ExecutionRunStatus.RUNNING:
            prior = None
        api_scope = execution_api_scope(snapshot)
        credential_error = _read_credential_snapshot_error(
            workspace_state,
            snapshot,
            current_read_credential_binding=current_read_credential_binding,
        )
        snapshot_error = execution_snapshot_message(execution_snapshot_issue(snapshot, connection_mode=workspace_state.odoo_connection_mode))
        scope_error = credential_error or snapshot_error
        assessment_hash = ""
        if workspace_state.source_mode is SourceMode.FILE:
            if current is None:
                assessment = self.preflight.current_business_intent_assessment(workspace_id, snapshot.semantic_hash)
                if assessment is None or not assessment.ready:
                    scope_error = credential_error or _business_intent_error(assessment)
                else:
                    assessment_hash = assessment.content_hash
            else:
                self.preflight.require_business_intent_approval(workspace_id, current)
        return build_execution_preview(
            snapshot,
            current_run=current,
            api_scope=api_scope,
            deferred_create_count=planned_deferred_create_count(snapshot, create_batch_rows=DEFAULT_CREATE_BATCH_ROWS),
            scope_error=scope_error,
            credential_refresh_required=bool(credential_error),
            prior_interrupted_run=prior,
            business_intent_assessment_hash=assessment_hash,
            business_intent=self.preflight.business_intent_review(workspace_id, snapshot.semantic_hash, actor=actor, run_id=current.run_id if current else "")
                if workspace_state.source_mode is SourceMode.FILE else None,
        )

    def current_review(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> ExecutionReviewProjection | None:
        """Validate the bounded Check changes projection for browser display."""

        projection = self.preflight.current_execution_review_projection(workspace_id)
        if projection is None:
            return None
        workspace_state = self.workspaces.get(workspace_id)
        validated = self.navigation_preview(
            ExecutionNavigationState(
                summary=projection.summary,
                execution_run_id=projection.current_run_id,
                execution_status=projection.current_execution_status,
                reconciliation_status=projection.reconciliation_status,
            ),
            workspace_state=workspace_state,
            actor=actor,
        )
        scope_error = validated.scope_error
        if workspace_state.source_mode is SourceMode.FILE and not projection.current_run_id:
            if projection.business_intent is not None:
                if not projection.business_intent.ready:
                    scope_error = projection.business_intent.message
            else:
                assessment = self.preflight.current_business_intent_assessment(workspace_id, projection.summary.snapshot_hash)
                if assessment is None or not assessment.ready:
                    scope_error = _business_intent_error(assessment)
        return projection.with_scope_error(
            scope_error,
            code=execution_scope_error_code(scope_error),
            credential_refresh_required=validated.credential_refresh_required,
        )

    def navigation_preview(
        self,
        state: ExecutionNavigationState,
        *,
        workspace_state: WorkspaceState,
        actor: Actor,
    ) -> ExecutionNavigationPreview:
        """Validate a bounded projection against the current target context."""

        summary = state.summary
        expected_target = target_identity_hash(
            connection_mode=(
                workspace_state.odoo_connection_mode.value
                if workspace_state.odoo_connection_mode is not None
                else ""
            ),
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
        )
        scope_error = ""
        credential_refresh_required = False
        if workspace_state.odoo_connection_mode not in {
            OdooConnectionMode.LOCAL,
            OdooConnectionMode.REMOTE,
        }:
            scope_error = "Configure the exact Odoo load target first"
        elif summary.target_hash != expected_target:
            scope_error = "The Odoo target changed; compare again"
        elif not summary.execution_shape_ready:
            scope_error = "The reviewed load shape needs attention"
        elif workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE:
            current_binding = (
                self.current_read_credential_binding(workspace_state, actor)
                if self.current_read_credential_binding is not None
                else None
            )
            identity_evidence = (
                summary.read_credential_binding_hash,
                summary.read_principal_hash,
                summary.read_permission_hash,
                summary.read_context_hash,
            )
            if current_binding is not None and not all(
                _SHA256.fullmatch(value) for value in identity_evidence
            ):
                scope_error = "Refresh the remote Odoo schema and compare again"
                credential_refresh_required = True
            elif current_binding is not None and not current_binding:
                scope_error = "Enter the current Odoo read key and compare again"
                credential_refresh_required = True
            elif (
                current_binding is not None
                and current_binding != summary.read_credential_binding_hash
            ):
                scope_error = "The Odoo read key changed; compare again"
                credential_refresh_required = True
        if workspace_state.source_mode is SourceMode.FILE and not state.execution_run_id and summary.business_intent_state in {"MISSING", "DEVIATION", "STALE"}:
            scope_error = scope_error or "Review business expectations against the proposed changes"
        return ExecutionNavigationPreview(
            state=state,
            scope_error=scope_error,
            credential_refresh_required=credential_refresh_required,
        )

    def close_interrupted_run_for_recomparison(
        self,
        workspace_id: str,
        *,
        expected_execution_run_id: str,
        expected_snapshot_hash: str,
        read_identity: OdooReadIdentity,
        read_credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        write_credential_binding_hash: str,
        actor: Actor,
    ) -> ExecutionRun:
        """Release an interrupted create load after an explicitly reviewed fresh comparison.

        Keep every original row and receipt. Closing the old attempt does not
        certify its outcome or write to Odoo; the new comparison owns any work
        subsequently confirmed by the operator.
        """
        def local_command() -> ExecutionRun:
            self.authorization.require(actor, Capability.EXPORT_PLAN_EXECUTE, workspace_id=workspace_id)
            workspace = self.workspaces.get(workspace_id)
            if workspace.source_mode is SourceMode.ODOO:
                raise WorkspaceError(
                    "Use the transfer recovery action for an Odoo-to-Odoo load",
                    load_failure_code="LOAD_USE_THE_TRANSFER_RECOVERY_ACTION_FOR_AN_ODOO_TO_ODOO_LOAD",
                )
            preview = self.current_preview(workspace_id, actor=actor)
            run = self.journal.get_current_run(workspace_id)
            if (
                preview is None or preview.snapshot.semantic_hash != expected_snapshot_hash
                or not preview.can_load or run is None
                or run.run_id != expected_execution_run_id
                or run.status is not ExecutionRunStatus.RUNNING
                or run.snapshot_hash == expected_snapshot_hash
            ):
                raise WorkspaceError(
                    "The interrupted load or fresh comparison is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_OR_FRESH_COMPARISON_IS_NO_LONGER_CURRENT",
                )
            _validate_read_identity(preview, read_identity, read_credential_binding_hash, required=True)
            _validate_write_identity(preview, write_identity, write_credential_binding_hash, required=True)
            if (
                run.target_hash != preview.snapshot.target_hash
                or run.target_database != preview.snapshot.target_database
                or write_credential_binding_hash != run.write_credential_binding_hash
                or write_identity.principal_hash != run.write_principal_hash
                or read_identity.principal_hash != write_identity.principal_hash
                or read_identity.context_hash != write_identity.context_hash
            ):
                raise WorkspaceError(
                    "Fresh recovery requires the original loading key and principal on the same target",
                    load_failure_code="LOAD_FRESH_RECOVERY_REQUIRES_THE_ORIGINAL_LOADING_KEY_AND_PRINCIPAL_ON_THE_SAME_TARGET",
                )
            original = self.preflight.execution_snapshot(workspace_id, run.preflight_run_id)
            def identity(row):
                # Preparation trace hashes include mapping and schema lineage, so
                # rebuilding the approved review changes them. Match the source
                # row and business keys, then check the exact saved Odoo receipts.
                return (row.dataset, row.source_row, row.target_model,
                        portable_key(row.source_identity), portable_key(row.business_identity),
                        portable_key(row.business_scope))
            fresh = {identity(row): row for row in preview.snapshot.rows}
            if len(fresh) != len(preview.snapshot.rows) or set(fresh) != {identity(row) for row in original.rows}:
                raise WorkspaceError(
                    "Fresh recovery must account for the same original prepared rows and identities",
                    load_failure_code="LOAD_FRESH_RECOVERY_MUST_ACCOUNT_FOR_THE_SAME_ORIGINAL_PREPARED_ROWS_AND_IDENTITIES",
                )
            original_by_id = {row.row_id: row for row in original.rows}
            for attempt in run.rows:
                row = original_by_id.get(attempt.row_id)
                if row is None or row.disposition != "CREATE" or attempt.operation != "CREATE":
                    raise WorkspaceError(
                        "Fresh recovery supports interrupted create loads only",
                        load_failure_code="LOAD_FRESH_RECOVERY_SUPPORTS_INTERRUPTED_CREATE_LOADS_ONLY",
                    )
                compared = fresh[identity(row)]
                if compared.disposition not in {"CREATE", "UPDATE", "UNCHANGED"} or compared.target_match_count != (
                    0 if compared.disposition == "CREATE" else 1
                ):
                    raise WorkspaceError(
                        "Fresh recovery requires an unambiguous current Odoo comparison",
                        load_failure_code="LOAD_FRESH_RECOVERY_REQUIRES_AN_UNAMBIGUOUS_CURRENT_ODOO_COMPARISON",
                    )
                if attempt.odoo_id is not None and compared.target_binding_hash != target_record_binding_hash(
                    attempt.target_model, attempt.odoo_id
                ):
                    raise WorkspaceError(
                        "Fresh comparison no longer matches an original Odoo receipt",
                        load_failure_code="LOAD_FRESH_COMPARISON_NO_LONGER_MATCHES_AN_ORIGINAL_ODOO_RECEIPT",
                    )
                if attempt.status in {ExecutionRowStatus.COMMITTED, ExecutionRowStatus.PARTIALLY_APPLIED} and (
                    attempt.odoo_id is None or compared.disposition == "CREATE"
                ):
                    raise WorkspaceError(
                        "An originally accepted Odoo record is missing from the fresh comparison",
                        load_failure_code="LOAD_AN_ORIGINALLY_ACCEPTED_ODOO_RECORD_IS_MISSING_FROM_THE_FRESH_COMPARISON",
                    )
            return self.journal.close_interrupted_run_for_recomparison(
                workspace_id, run, preview.snapshot.preflight_run_id, actor=actor,
            )

        return self._local_execution_check(local_command)

    def require_business_intent(
        self, workspace_id: str, *, expected_snapshot_hash: str,
        expected_assessment_hash: str, actor: Actor, recovery_run_id: str = "",
    ) -> None:
        """Recheck the confirmed sidecar before enqueue or writer construction."""
        self.authorization.require(actor, Capability.EXPORT_PLAN_EXECUTE, workspace_id=workspace_id)
        if recovery_run_id:
            run = self.journal.get_run(workspace_id, recovery_run_id)
            if run is None or run.snapshot_hash != expected_snapshot_hash or run.status is not ExecutionRunStatus.RUNNING:
                raise WorkspaceError(
                    "The interrupted load changed before recovery",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_CHANGED_BEFORE_RECOVERY",
                )
            self.preflight.require_business_intent_approval(workspace_id, run)
            return
        assessment = self.preflight.current_business_intent_assessment(workspace_id, expected_snapshot_hash)
        if assessment is None or not assessment.ready:
            raise WorkspaceError(
                _business_intent_error(assessment),
                load_failure_code="LOAD_BUSINESS_EXPECTATIONS_NOT_READY",
            )
        if not expected_assessment_hash or assessment.content_hash != expected_assessment_hash:
            raise WorkspaceError(
                "Business expectations changed after confirmation; review them again",
                load_failure_code="LOAD_BUSINESS_EXPECTATIONS_CHANGED_AFTER_CONFIRMATION_REVIEW_THEM_AGAIN",
            )

    def execute(
        self,
        workspace_id: str,
        *,
        expected_snapshot_hash: str,
        executor: OdooWriteExecutor,
        actor: Actor,
        batch_rows: int | str = DEFAULT_CREATE_BATCH_ROWS,
        read_identity: OdooReadIdentity | None = None,
        read_credential_binding_hash: str = "",
        write_identity: OdooWriteIdentity | None = None,
        write_credential_binding_hash: str = "",
        progress: Callable[[ExecutionProgress], None] | None = None,
        expected_intent_assessment_hash: str | None = None,
        rejection_policy: RejectionPolicy = RejectionPolicy.STOP,
    ) -> ExecutionRun:
        """Start and carry out a reviewed conventional Odoo load.

        Called by the confirmed-load worker after its browser job is queued.
        It revalidates the immutable preflight snapshot, target identity,
        credentials, and authority before journaling any remote write; the
        lower-level ``_continue_run`` owns batch transport and receipts.
        """
        def admit() -> _ExecutionAdmission:
            self.authorization.require(
                actor,
                Capability.EXPORT_PLAN_EXECUTE,
                workspace_id=workspace_id,
            )
            workspace_state = self.workspaces.get(workspace_id)
            if workspace_state.source_mode is SourceMode.ODOO:
                raise WorkspaceError(
                    "Use the confirmed transfer route for an Odoo-to-Odoo load.",
                    load_failure_code="LOAD_USE_THE_CONFIRMED_TRANSFER_ROUTE_FOR_AN_ODOO_TO_ODOO_LOAD",
                )
            create_batch_rows = validated_create_batch_rows(batch_rows)
            preview = self.current_preview(workspace_id, actor=actor)
            if preview is None:
                raise WorkspaceError(
                    "Compare the prepared data with Odoo first",
                    load_failure_code="LOAD_PREVIEW_MISSING",
                )
            snapshot = preview.snapshot
            observe_load_context(snapshot, source_mode=workspace_state.source_mode.value, connection_mode=workspace_state.odoo_connection_mode.value if workspace_state.odoo_connection_mode is not None else "")
            if snapshot.semantic_hash != expected_snapshot_hash:
                raise WorkspaceError(
                    "The load preview changed. Review it again.",
                    load_failure_code="LOAD_PREVIEW_CHANGED",
                )
            if preview.scope_error:
                raise WorkspaceError(
                    preview.scope_error,
                    load_failure_code="LOAD_SCOPE_CHECK_FAILED",
                )
            if expected_intent_assessment_hash is not None and expected_intent_assessment_hash != preview.business_intent_assessment_hash:
                raise WorkspaceError(
                    "Business expectations changed after confirmation; review them again",
                    load_failure_code="LOAD_BUSINESS_EXPECTATIONS_CHANGED_AFTER_CONFIRMATION_REVIEW_THEM_AGAIN",
                )
            self._validate_execution_scope(workspace_state, preview, executor)
            _validate_write_identity(
                preview,
                write_identity,
                write_credential_binding_hash,
                required=(
                    self.require_remote_write_identity
                    and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
                ),
            )
            _validate_read_identity(
                preview,
                read_identity,
                read_credential_binding_hash,
                required=(
                    self.require_remote_read_identity
                    and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
                ),
            )

            return _ExecutionAdmission(workspace_state, preview, create_batch_rows)

        admission = self._local_execution_check(admit)
        workspace_state = admission.workspace_state
        preview = admission.preview
        snapshot = preview.snapshot
        create_batch_rows = admission.batch_rows

        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = resolve_identity_crosswalk(
            write_rows,
            metadata,
            by_source,
            executor,
        )
        attempts = tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(intent.field for intent in row.fields),
                proposed_external_id=row.proposed_external_id,
                schedule_component=row.schedule_component,
            )
            for row in write_rows
        )
        started_at = datetime.now(UTC)
        run = ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=create_batch_rows,
            status=ExecutionRunStatus.RUNNING,
            started_at=started_at,
            started_by=actor.identity.display_name,
            completed_at=None,
            rows=attempts,
            write_credential_binding_hash=write_credential_binding_hash,
            write_principal_hash=(
                write_identity.principal_hash if write_identity is not None else ""
            ),
            write_permission_hash=(
                write_identity.permission_hash if write_identity is not None else ""
            ),
            write_context_hash=(
                write_identity.context_hash if write_identity is not None else ""
            ),
        )
        if self.load_controls is not None:
            control = LoadControl(policy=rejection_policy)
            self._local_execution_check(
                lambda: self.journal.start_run(
                    workspace_id, run, actor=actor,
                    intent_assessment_hash=preview.business_intent_assessment_hash,
                    load_control=control,
                )
            )
            run = replace(run, load_control=control)
        else:
            if rejection_policy is not RejectionPolicy.STOP:
                raise WorkspaceError(
                    "Load continuation is unavailable for this journal",
                    load_failure_code="LOAD_LOAD_CONTINUATION_IS_UNAVAILABLE_FOR_THIS_JOURNAL",
                )
            self._local_execution_check(
                lambda: self.journal.start_run(
                    workspace_id, run, actor=actor,
                    intent_assessment_hash=preview.business_intent_assessment_hash,
                )
            )
        return self._continue_run(
            workspace_state,
            snapshot,
            run,
            executor,
            actor,
            identity_cache=identity_cache,
            progress=progress,
        )

    def current_transfer_run(self, workspace_id: str) -> ExecutionRun | None:
        """Return the current durable journal for an Odoo-source transfer."""

        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.source_mode is not SourceMode.ODOO:
            return None
        return self.journal.get_current_run(workspace_id)

    def execute_transfer(
        self,
        workspace_id: str,
        *,
        expected_snapshot_hash: str,
        expected_preflight_hash: str,
        snapshot: ExecutionSnapshot,
        executor: OdooWriteExecutor,
        actor: Actor,
        batch_rows: int | str = DEFAULT_CREATE_BATCH_ROWS,
        read_identity: OdooReadIdentity,
        credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        protected_values: Mapping[str, Any] | None = None,
        progress: Callable[[ExecutionProgress], None] | None = None,
    ) -> ExecutionRun:
        """Start a governed Odoo-to-Odoo transfer from a qualified source.

        Called by the transfer-load worker. It shares the journal and write
        safeguards of ``execute`` while additionally validates the frozen
        source-finalization and transfer-preflight evidence.
        """
        """Enter the shared writer from a current, confirmed transfer snapshot."""

        def admit() -> _ExecutionAdmission:
            self.authorization.require(
                actor,
                Capability.EXPORT_PLAN_EXECUTE,
                workspace_id=workspace_id,
            )
            source_workspace = self.workspaces.get(workspace_id)
            if source_workspace.source_mode is not SourceMode.ODOO:
                raise WorkspaceError(
                    "The destination load requires a frozen Odoo source",
                    load_failure_code="LOAD_THE_DESTINATION_LOAD_REQUIRES_A_FROZEN_ODOO_SOURCE",
                )
            report = source_workspace.transfer_preflight_report
            if (
                report is None
                or not source_workspace.transfer_preflight_ready(
                    source_selection_hash=report.source_selection_hash,
                    source_schema_hash=report.source_schema_hash,
                )
                or not report.ready
                or report.content_hash != expected_preflight_hash
                or snapshot.preflight_result_hash != report.content_hash
                or snapshot.workspace_id != workspace_id
                or snapshot.semantic_hash != expected_snapshot_hash
            ):
                raise WorkspaceError(
                    "The destination preflight changed. Run it again before loading.",
                    load_failure_code="LOAD_THE_DESTINATION_PREFLIGHT_CHANGED_RUN_IT_AGAIN_BEFORE_LOADING",
                )
            destination_workspace = transfer_destination_workspace(source_workspace)
            observe_load_context(snapshot, source_mode="ODOO", connection_mode=destination_workspace.odoo_connection_mode.value if destination_workspace.odoo_connection_mode is not None else "")
            create_batch_rows = validated_create_batch_rows(batch_rows)
            current = self.journal.get_current_run(workspace_id)
            preview = ExecutionPreview(
                snapshot=snapshot,
                datasets=(),
                current_run=current,
                api_scope=execution_api_scope(snapshot),
                deferred_create_count=planned_deferred_create_count(
                    snapshot,
                    create_batch_rows=create_batch_rows,
                ),
                scope_error=execution_snapshot_message(execution_snapshot_issue(snapshot, connection_mode=destination_workspace.odoo_connection_mode)),
            )
            if current is not None:
                raise WorkspaceError(
                    "This approved transfer already has a load journal. Verify its outcome.",
                    load_failure_code="LOAD_THIS_APPROVED_TRANSFER_ALREADY_HAS_A_LOAD_JOURNAL_VERIFY_ITS_OUTCOME",
                )
            self._validate_execution_scope(destination_workspace, preview, executor)
            _validate_read_identity(
                preview,
                read_identity,
                credential_binding_hash,
                required=True,
            )
            _validate_write_identity(
                preview,
                write_identity,
                credential_binding_hash,
                required=True,
            )
            if (
                write_identity.principal_hash != read_identity.principal_hash
                or write_identity.context_hash != read_identity.context_hash
            ):
                raise WorkspaceError(
                    "The destination transfer key changed principal or company context",
                    load_failure_code="LOAD_THE_DESTINATION_TRANSFER_KEY_CHANGED_PRINCIPAL_OR_COMPANY_CONTEXT",
                )

            return _ExecutionAdmission(destination_workspace, preview, create_batch_rows)

        admission = self._local_execution_check(admit)
        destination_workspace = admission.workspace_state
        create_batch_rows = admission.batch_rows

        write_rows = tuple(
            row for row in snapshot.rows if row.disposition in {"CREATE", "UPDATE"}
        )
        if not write_rows:
            raise WorkspaceError(
                "This approved transfer has no records to load",
                load_failure_code="LOAD_THIS_APPROVED_TRANSFER_HAS_NO_RECORDS_TO_LOAD",
            )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        assert_create_rows_still_absent(write_rows, metadata, executor)
        identity_cache = resolve_identity_crosswalk(
            write_rows,
            metadata,
            by_source,
            executor,
        )
        attempts = tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(intent.field for intent in row.fields),
                proposed_external_id=row.proposed_external_id,
                schedule_component=row.schedule_component,
            )
            for row in write_rows
        )
        run = ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=create_batch_rows,
            status=ExecutionRunStatus.RUNNING,
            started_at=datetime.now(UTC),
            started_by=actor.identity.display_name,
            completed_at=None,
            rows=attempts,
            write_credential_binding_hash=credential_binding_hash,
            write_principal_hash=write_identity.principal_hash,
            write_permission_hash=write_identity.permission_hash,
            write_context_hash=write_identity.context_hash,
        )
        self._local_execution_check(
            lambda: self.journal.start_run(
                workspace_id,
                run,
                actor=actor,
                transfer_preflight_hash=expected_preflight_hash,
            )
        )
        return self._continue_run(
            destination_workspace,
            snapshot,
            run,
            executor,
            actor,
            identity_cache=identity_cache,
            protected_values=protected_values,
            progress=progress,
            create_with_external_ids=True,
        )

    def resume(
        self,
        workspace_id: str,
        *,
        expected_execution_run_id: str,
        recovery: ReconciliationRun,
        executor: OdooWriteExecutor,
        actor: Actor,
        write_identity: OdooWriteIdentity | None = None,
        write_credential_binding_hash: str = "",
        progress: Callable[[ExecutionProgress], None] | None = None,
        expected_load_control_hash: str = "",
    ) -> ExecutionRun:
        """Recover an interrupted conventional load before attempting retries.

        Called by the confirmed-load worker for an active interrupted journal.
        It delegates read-back classification to reconciliation, persists that
        recovery, and only then calls ``_continue_run`` for retry-safe rows.
        """
        """Resume an interrupted journal only from exact read-back evidence."""

        def admit() -> _RecoveryAdmission:
            self.authorization.require(
                actor,
                Capability.EXPORT_PLAN_EXECUTE,
                workspace_id=workspace_id,
            )
            workspace_state = self.workspaces.get(workspace_id)
            if workspace_state.source_mode is SourceMode.ODOO:
                raise WorkspaceError(
                    "Use the transfer recovery action for an interrupted "
                    "Odoo-to-Odoo load.",
                    load_failure_code="LOAD_USE_THE_TRANSFER_RECOVERY_ACTION_FOR_AN_INTERRUPTED_ODOO_TO_ODOO_LOAD",
                )
            snapshot = self.preflight.current_execution_snapshot(workspace_id)
            if snapshot is None:
                raise WorkspaceError(
                    "The interrupted load preview is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_PREVIEW_IS_NO_LONGER_CURRENT",
                )
            if not assess_odoo_operation(
                snapshot.target_odoo_version, OdooOperation.RECOVER,
            ).allowed:
                raise WorkspaceError(
                    "This Odoo version is not enabled for the schema-bound load path",
                    load_failure_code="LOAD_THIS_ODOO_VERSION_IS_NOT_ENABLED_FOR_THE_SCHEMA_BOUND_LOAD_PATH",
                )
            observe_load_context(snapshot, source_mode=workspace_state.source_mode.value, connection_mode=workspace_state.odoo_connection_mode.value if workspace_state.odoo_connection_mode is not None else "")
            run = self.journal.get_run(workspace_id, expected_execution_run_id)
            current = self.journal.get_current_run(workspace_id, snapshot.semantic_hash)
            if (
                run is None
                or current is None
                or current.run_id != expected_execution_run_id
                or run.status is not ExecutionRunStatus.RUNNING
                or run.snapshot_hash != snapshot.semantic_hash
                or run.snapshot_root_hash != snapshot.root_hash
                or run.preflight_run_id != snapshot.preflight_run_id
                or run.target_hash != snapshot.target_hash
                or run.target_database != snapshot.target_database
            ):
                raise WorkspaceError(
                    "The interrupted load or preview is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_OR_PREVIEW_IS_NO_LONGER_CURRENT",
                )
            self.preflight.require_business_intent_approval(workspace_id, run)
            paused_control = run.load_control
            paused = paused_control is not None and paused_control.state is LoadControlState.PAUSED
            if paused_control is not None and paused and (
                self.load_controls is None
                or expected_load_control_hash != paused_control.content_hash
            ):
                raise WorkspaceError(
                    "Review the paused load before continuing the next group",
                    load_failure_code="LOAD_REVIEW_THE_PAUSED_LOAD_BEFORE_CONTINUING_THE_NEXT_GROUP",
                )
            if executor.target_hash != snapshot.target_hash:
                raise WorkspaceError(
                    "The recovery writer points to a different Odoo target",
                    load_failure_code="LOAD_THE_RECOVERY_WRITER_POINTS_TO_A_DIFFERENT_ODOO_TARGET",
                )
            if executor.scope_hash != execution_api_scope(snapshot).semantic_hash:
                raise WorkspaceError(
                    "The recovery writer is not bound to this reviewed load preview",
                    load_failure_code="LOAD_THE_RECOVERY_WRITER_IS_NOT_BOUND_TO_THIS_REVIEWED_LOAD_PREVIEW",
                )
            _require_resume_write_identity(
                run,
                write_identity,
                write_credential_binding_hash,
                required=(
                    self.require_remote_write_identity
                    and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
                ),
            )
            checked_recovery = ReconciliationRun.from_json(recovery.to_json())
            recovery_hash = checked_recovery.semantic_hash
            already_applied = (
                not run.in_flight_count
                and all(
                    item.recovery_hash == recovery_hash
                    for item in run.rows
                )
            )
            if not already_applied:
                recovered = classify_recovery(
                    snapshot, run, checked_recovery, targeted=True,
                )
                self.journal.record_recovery(
                    workspace_id,
                    run.run_id,
                    recovered,
                    actor=actor,
                )
                run = self.journal.get_run(workspace_id, run.run_id)
                if run is None:
                    raise WorkspaceError(
                        "The recovered load journal could not be reloaded",
                        load_failure_code="LOAD_THE_RECOVERED_LOAD_JOURNAL_COULD_NOT_BE_RELOADED",
                    )

            if paused:
                if self.load_controls is None:
                    raise WorkspaceError(
                        "This load has no durable continuation control",
                        load_failure_code="LOAD_THIS_LOAD_HAS_NO_DURABLE_CONTINUATION_CONTROL",
                    )
                control = self.load_controls.resume_load_control(
                    workspace_id, run.run_id, expected_hash=expected_load_control_hash, actor=actor,
                )
                run = replace(run, load_control=control)

            return _RecoveryAdmission(workspace_state, snapshot, run)

        admission = self._local_execution_check(admit)
        workspace_state = admission.workspace_state
        snapshot = admission.snapshot
        run = admission.run

        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = resolve_identity_crosswalk(
            write_rows,
            metadata,
            by_source,
            executor,
        )
        return self._continue_run(
            workspace_state,
            snapshot,
            run,
            executor,
            actor,
            identity_cache=identity_cache,
            progress=progress,
        )

    def review_recovery(
        self,
        workspace_id: str,
        *,
        expected_execution_run_id: str,
        recovery: ReconciliationRun,
        actor: Actor,
        write_identity: OdooWriteIdentity | None = None,
        write_credential_binding_hash: str = "",
    ) -> RecoveryResumePlan:
        """Validate read-only recovery evidence and summarize the safe resume."""

        def local_command() -> RecoveryResumePlan:
            self.authorization.require(
                actor,
                Capability.EXPORT_PLAN_EXECUTE,
                workspace_id=workspace_id,
            )
            workspace_state = self.workspaces.get(workspace_id)
            if workspace_state.source_mode is SourceMode.ODOO:
                raise WorkspaceError(
                    "Use the transfer recovery action for an interrupted "
                    "Odoo-to-Odoo load.",
                    load_failure_code="LOAD_USE_THE_TRANSFER_RECOVERY_ACTION_FOR_AN_INTERRUPTED_ODOO_TO_ODOO_LOAD",
                )
            snapshot = self.preflight.current_execution_snapshot(workspace_id)
            if snapshot is None:
                raise WorkspaceError(
                    "The interrupted load preview is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_PREVIEW_IS_NO_LONGER_CURRENT",
                )
            if not assess_odoo_operation(
                snapshot.target_odoo_version,
                OdooOperation.RECOVER,
            ).allowed:
                raise WorkspaceError(
                    "This Odoo version is not enabled for the schema-bound load path",
                    load_failure_code="LOAD_THIS_ODOO_VERSION_IS_NOT_ENABLED_FOR_THE_SCHEMA_BOUND_LOAD_PATH",
                )
            run = self.journal.get_run(workspace_id, expected_execution_run_id)
            current = self.journal.get_current_run(workspace_id, snapshot.semantic_hash)
            if (
                run is None
                or current is None
                or current.run_id != expected_execution_run_id
                or run.status is not ExecutionRunStatus.RUNNING
                or run.snapshot_hash != snapshot.semantic_hash
                or run.snapshot_root_hash != snapshot.root_hash
                or run.preflight_run_id != snapshot.preflight_run_id
                or run.target_hash != snapshot.target_hash
                or run.target_database != snapshot.target_database
            ):
                raise WorkspaceError(
                    "The interrupted load or preview is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_LOAD_OR_PREVIEW_IS_NO_LONGER_CURRENT",
                )
            _require_resume_write_identity(
                run,
                write_identity,
                write_credential_binding_hash,
                required=(
                    self.require_remote_write_identity
                    and workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
                ),
            )
            classified = classify_recovery(
                snapshot,
                run,
                ReconciliationRun.from_json(recovery.to_json()),
                targeted=True,
            )
            original = {item.row_id: item for item in run.rows}
            confirmed_after_interruption_count = sum(
                original[item.row_id].status is ExecutionRowStatus.IN_FLIGHT
                and item.status is ExecutionRowStatus.COMMITTED
                for item in classified
            )
            safe_to_retry_count = sum(
                original[item.row_id].status is ExecutionRowStatus.IN_FLIGHT
                and item.status is ExecutionRowStatus.RETRY_READY
                for item in classified
            )
            relationship_pending_count = sum(
                item.status is ExecutionRowStatus.PARTIALLY_APPLIED
                for item in classified
            )
            resume_count = sum(
                item.diagnostic is None and item.status
                in {
                    ExecutionRowStatus.PLANNED,
                    ExecutionRowStatus.RETRY_READY,
                    ExecutionRowStatus.PARTIALLY_APPLIED,
                }
                for item in classified
            )
            return RecoveryResumePlan(
                execution_run_id=run.run_id,
                snapshot_hash=snapshot.semantic_hash,
                total_count=run.total_count,
                already_accepted_count=run.committed_count,
                waiting_count=run.planned_count + run.retry_ready_count,
                uncertain_count=run.in_flight_count + run.unknown_count,
                confirmed_after_interruption_count=confirmed_after_interruption_count,
                safe_to_retry_count=safe_to_retry_count,
                relationship_pending_count=relationship_pending_count,
                resume_count=resume_count,
                load_control_hash=run.load_control.content_hash if run.load_control else "",
            )

        return self._local_execution_check(local_command)

    def resume_transfer(
        self,
        workspace_id: str,
        *,
        expected_execution_run_id: str,
        expected_snapshot_hash: str,
        expected_preflight_hash: str,
        snapshot: ExecutionSnapshot,
        recovery: ReconciliationRun,
        executor: OdooWriteExecutor,
        actor: Actor,
        read_identity: OdooReadIdentity,
        credential_binding_hash: str,
        write_identity: OdooWriteIdentity,
        protected_values: Mapping[str, Any] | None = None,
        progress: Callable[[ExecutionProgress], None] | None = None,
    ) -> ExecutionRun:
        """Recover an interrupted Odoo-to-Odoo transfer after source rechecks.

        Called by the transfer-load worker. It mirrors ``resume`` but preserves
        transfer-specific source evidence and external-ID create semantics.
        """
        """Resume one interrupted transfer from exact same-key read-back."""

        def admit() -> _RecoveryAdmission:
            self.authorization.require(
                actor,
                Capability.EXPORT_PLAN_EXECUTE,
                workspace_id=workspace_id,
            )
            source_workspace = self.workspaces.get(workspace_id)
            report = source_workspace.transfer_preflight_report
            if (
                source_workspace.source_mode is not SourceMode.ODOO
                or report is None
                or not source_workspace.transfer_preflight_ready(
                    source_selection_hash=report.source_selection_hash,
                    source_schema_hash=report.source_schema_hash,
                )
                or not report.ready
                or report.content_hash != expected_preflight_hash
                or snapshot.workspace_id != workspace_id
                or snapshot.semantic_hash != expected_snapshot_hash
                or snapshot.preflight_result_hash != report.content_hash
            ):
                raise WorkspaceError(
                    "The interrupted transfer preview or preflight is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_TRANSFER_PREVIEW_OR_PREFLIGHT_IS_NO_LONGER_CURRENT",
                )
            run = self.journal.get_run(workspace_id, expected_execution_run_id)
            current = self.journal.get_current_run(workspace_id, snapshot.semantic_hash)
            if (
                run is None
                or current is None
                or current.run_id != expected_execution_run_id
                or run.status is not ExecutionRunStatus.RUNNING
                or run.snapshot_hash != snapshot.semantic_hash
                or run.snapshot_root_hash != snapshot.root_hash
                or run.preflight_run_id != snapshot.preflight_run_id
                or run.target_hash != snapshot.target_hash
                or run.target_database != snapshot.target_database
            ):
                raise WorkspaceError(
                    "The interrupted transfer journal is no longer current",
                    load_failure_code="LOAD_THE_INTERRUPTED_TRANSFER_JOURNAL_IS_NO_LONGER_CURRENT",
                )

            destination_workspace = transfer_destination_workspace(source_workspace)
            observe_load_context(snapshot, source_mode="ODOO", connection_mode=destination_workspace.odoo_connection_mode.value if destination_workspace.odoo_connection_mode is not None else "")
            preview = ExecutionPreview(
                snapshot=snapshot,
                datasets=(),
                current_run=run,
                api_scope=execution_api_scope(snapshot),
                deferred_create_count=0,
                scope_error=execution_snapshot_message(execution_snapshot_issue(snapshot, connection_mode=destination_workspace.odoo_connection_mode, operation=OdooOperation.RECOVER)),
            )
            if preview.scope_error:
                raise WorkspaceError(
                    preview.scope_error,
                    load_failure_code="LOAD_SCOPE_CHECK_FAILED",
                )
            if executor.target_hash != snapshot.target_hash:
                raise WorkspaceError(
                    "The recovery writer points to a different Odoo target",
                    load_failure_code="LOAD_THE_RECOVERY_WRITER_POINTS_TO_A_DIFFERENT_ODOO_TARGET",
                )
            if executor.scope_hash != preview.api_scope.semantic_hash:
                raise WorkspaceError(
                    "The recovery writer is not bound to this transfer preview",
                    load_failure_code="LOAD_THE_RECOVERY_WRITER_IS_NOT_BOUND_TO_THIS_TRANSFER_PREVIEW",
                )
            _validate_read_identity(
                preview,
                read_identity,
                credential_binding_hash,
                required=True,
            )
            _validate_write_identity(
                preview,
                write_identity,
                credential_binding_hash,
                required=True,
            )
            _require_resume_write_identity(
                run,
                write_identity,
                credential_binding_hash,
                required=True,
            )
            if (
                write_identity.principal_hash != read_identity.principal_hash
                or write_identity.context_hash != read_identity.context_hash
            ):
                raise WorkspaceError(
                    "The destination transfer key changed principal or company context",
                    load_failure_code="LOAD_THE_DESTINATION_TRANSFER_KEY_CHANGED_PRINCIPAL_OR_COMPANY_CONTEXT",
                )
            if recovery.verification_credential_binding_hash != credential_binding_hash:
                raise WorkspaceError(
                    "The recovery evidence was collected with another destination "
                    "transfer key",
                    load_failure_code="LOAD_THE_RECOVERY_EVIDENCE_WAS_COLLECTED_WITH_ANOTHER_DESTINATION_TRANSFER_KEY",
                )

            return _RecoveryAdmission(destination_workspace, snapshot, run)

        admission = self._local_execution_check(admit)
        destination_workspace = admission.workspace_state
        run = admission.run

        recovery = ReconciliationRun.from_json(recovery.to_json())
        recovery_hash = recovery.semantic_hash
        already_applied = (
            not run.in_flight_count
            and all(item.recovery_hash == recovery_hash for item in run.rows)
        )
        recovered = (
            run.rows
            if already_applied
            else classify_recovery(snapshot, run, recovery)
        )
        write_rows = tuple(
            row for row in snapshot.rows if row.disposition in {"CREATE", "UPDATE"}
        )
        row_by_id = {row.row_id: row for row in write_rows}
        retryable_creates = tuple(
            row_by_id[item.row_id]
            for item in recovered
            if item.status
            in {ExecutionRowStatus.PLANNED, ExecutionRowStatus.RETRY_READY}
            and row_by_id[item.row_id].disposition == "CREATE"
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        assert_create_rows_still_absent(
            retryable_creates,
            metadata,
            executor,
        )
        if not already_applied:
            def publish_recovery() -> ExecutionRun:
                self.journal.record_recovery(
                    workspace_id,
                    run.run_id,
                    recovered,
                    actor=actor,
                )
                saved = self.journal.get_run(workspace_id, run.run_id)
                if saved is None:
                    raise WorkspaceError(
                        "The recovered transfer journal could not be reloaded",
                        load_failure_code="LOAD_THE_RECOVERED_TRANSFER_JOURNAL_COULD_NOT_BE_RELOADED",
                    )
                return saved

            run = self._local_execution_check(publish_recovery)

        by_source = {
            (row.dataset, portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = resolve_identity_crosswalk(
            write_rows,
            metadata,
            by_source,
            executor,
        )
        return self._continue_run(
            destination_workspace,
            snapshot,
            run,
            executor,
            actor,
            identity_cache=identity_cache,
            protected_values=protected_values,
            progress=progress,
            create_with_external_ids=True,
        )

    def request_stop(self, workspace_id: str, run_id: str, *, actor: Actor) -> LoadControl:
        """Authorize a durable stop for the exact current load."""
        def local_command() -> LoadControl:
            self.authorization.require(actor, Capability.EXPORT_PLAN_EXECUTE, workspace_id=workspace_id)
            if self.load_controls is None:
                raise WorkspaceError(
                    "Durable load stopping is unavailable",
                    load_failure_code="LOAD_DURABLE_LOAD_STOPPING_IS_UNAVAILABLE",
                )
            control = self.load_controls.request_load_stop(workspace_id, run_id, actor=actor)
            saved = self.journal.get_run(workspace_id, run_id)
            if saved is not None and not saved.in_flight_count and saved.status is ExecutionRunStatus.RUNNING:
                pending = tuple(replace(row, status=ExecutionRowStatus.BLOCKED,
                    safe_error="Not attempted because the operator stopped further uploads")
                    for row in saved.rows if row.status in {ExecutionRowStatus.PLANNED, ExecutionRowStatus.RETRY_READY})
                if pending:
                    JournalTransitions(
                        self.journal, {}, local_operation=self._run_local,
                    ).record_outcomes(workspace_id, run_id, pending)
                self.journal.finish_run(workspace_id, run_id, ExecutionRunStatus.COMPLETED_WITH_ERRORS, actor=actor)
            return control

        return self._local_execution_check(local_command)

    def _continue_run(
        self, workspace_state: WorkspaceState, snapshot: ExecutionSnapshot,
        run: ExecutionRun, executor: OdooWriteExecutor, actor: Actor, *,
        identity_cache: dict[str, int], progress: Callable[[ExecutionProgress], None] | None,
        protected_values: Mapping[str, Any] | None = None, create_with_external_ids: bool = False,
    ) -> ExecutionRun:
        """End new transport at a durable pause/stop while retaining all receipts."""
        observe_load_journal(run, projection="INITIAL")
        publisher = ExecutionProgressPublisher(run, progress, row_interval=min(validated_create_batch_rows(run.batch_rows or 0), MAX_PROGRESS_ROWS_BETWEEN_UPDATES))
        try:
            return LoadWriter(
                self.journal, self.load_controls, local_operation=self._run_local,
            ).run(workspace_state, snapshot, run, executor, actor,
                identity_cache=identity_cache, progress=publisher, protected_values=protected_values,
                create_with_external_ids=create_with_external_ids)
        except OdooRequestDeferred:
            saved = self._local_execution_check(
                lambda: self.journal.get_run(run.workspace_id, run.run_id)
            )
            if saved is None or saved.in_flight_count:
                raise
            if progress is not None:
                if saved.load_control is not None and saved.load_control.state is LoadControlState.STOPPED:
                    publisher.reconcile_stopped_run(saved)
                else:
                    publisher.finish(saved)
            return saved
        except (LoadHalted, WorkspaceError) as error:
            failure = error

            def settle_halt() -> _SettledHalt:
                saved = self.journal.get_run(run.workspace_id, run.run_id)
                if isinstance(failure, WorkspaceError) and not (
                    saved is not None and saved.load_control is not None
                    and saved.load_control.state is LoadControlState.STOPPED
                    and saved.status is ExecutionRunStatus.COMPLETED_WITH_ERRORS
                    and not saved.in_flight_count
                ):
                    raise failure
                if saved is None or saved.load_control is None or saved.in_flight_count:
                    raise WorkspaceError(
                        "The halted load needs read-back before continuation",
                        load_failure_code="LOAD_THE_HALTED_LOAD_NEEDS_READ_BACK_BEFORE_CONTINUATION",
                    ) from None
                if saved.status is not ExecutionRunStatus.RUNNING:
                    return _SettledHalt(saved, was_terminal=True)
                if saved.load_control.state is LoadControlState.STOP_REQUESTED:
                    recorded = {row.row_id: row for row in saved.rows}
                    pending = tuple(row for row in snapshot.rows if row.row_id in recorded
                        and recorded[row.row_id].status in {ExecutionRowStatus.PLANNED, ExecutionRowStatus.RETRY_READY})
                    JournalTransitions(
                        self.journal, recorded, publisher, local_operation=self._run_local,
                    ).record_blocked(run.workspace_id, run.run_id, pending, recorded,
                        "Not attempted because the operator stopped further uploads")
                    saved = self.journal.finish_run(run.workspace_id, run.run_id,
                        ExecutionRunStatus.COMPLETED_WITH_ERRORS, actor=actor)
                return _SettledHalt(saved, was_terminal=False)

            settled = self._local_execution_check(settle_halt)
            saved = settled.run
            if progress is not None:
                if settled.was_terminal and saved.load_control is not None and saved.load_control.state is LoadControlState.STOPPED:
                    publisher.reconcile_stopped_run(saved)
                else:
                    publisher.finish(saved)
            return saved
        finally:
            if current_load_observation() is not None:
                try:
                    observed_run = self._local_execution_check(
                        lambda: self.journal.get_run(run.workspace_id, run.run_id)
                    )
                except Exception as capture_error:  # noqa: BLE001 - optional evidence read
                    emit_load_evidence("capture_failed", exception_class=type(capture_error).__name__)
                else:
                    if observed_run is not None:
                        observe_load_journal(observed_run, projection="FINAL")
                    else:
                        emit_load_evidence("capture_failed", reason="JOURNAL_UNAVAILABLE")

    def complete_no_changes(
        self,
        workspace_id: str,
        *,
        expected_snapshot_hash: str,
        actor: Actor,
        expected_intent_assessment_hash: str | None = None,
    ) -> ExecutionRun:
        """Record completion for a reviewed snapshot with no Odoo writes.

        Called by the no-changes confirmation route. It still validates target,
        snapshot, and authorization so a stale or blocked comparison cannot be
        marked complete merely because its write count is zero.
        """
        """Record a reviewed zero-write comparison without contacting Odoo."""

        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.source_mode is SourceMode.ODOO:
            raise WorkspaceError(
                "Use the transfer outcome to complete an Odoo-to-Odoo load.",
                load_failure_code="LOAD_USE_THE_TRANSFER_OUTCOME_TO_COMPLETE_AN_ODOO_TO_ODOO_LOAD",
            )
        preview = self.current_preview(workspace_id, actor=actor)
        if preview is None:
            raise WorkspaceError(
                "Compare the prepared data with Odoo first",
                load_failure_code="LOAD_PREVIEW_MISSING",
            )
        snapshot = preview.snapshot
        if snapshot.semantic_hash != expected_snapshot_hash:
            raise WorkspaceError(
                "The comparison changed. Review it again.",
                load_failure_code="LOAD_THE_COMPARISON_CHANGED_REVIEW_IT_AGAIN",
            )
        if preview.current_run is not None:
            if (
                preview.current_run.status is ExecutionRunStatus.COMPLETED
                and preview.current_run.total_count == 0
            ):
                return preview.current_run
            raise WorkspaceError(
                "This comparison already has a load result. Review that result first.",
                load_failure_code="LOAD_THIS_COMPARISON_ALREADY_HAS_A_LOAD_RESULT_REVIEW_THAT_RESULT_FIRST",
            )
        if preview.scope_error and preview.scope_error != NO_WRITE_ROWS_MESSAGE:
            raise WorkspaceError(
                preview.scope_error,
                load_failure_code="LOAD_SCOPE_CHECK_FAILED",
            )
        if expected_intent_assessment_hash is not None and expected_intent_assessment_hash != preview.business_intent_assessment_hash:
            raise WorkspaceError(
                "Business expectations changed after confirmation; review them again",
                load_failure_code="LOAD_BUSINESS_EXPECTATIONS_CHANGED_AFTER_CONFIRMATION_REVIEW_THEM_AGAIN",
            )
        if not preview.can_complete_without_load:
            raise WorkspaceError(
                "Only a complete comparison with no proposed Odoo changes can finish here",
                load_failure_code="LOAD_ONLY_A_COMPLETE_COMPARISON_WITH_NO_PROPOSED_ODOO_CHANGES_CAN_FINISH_HERE",
            )
        started_at = datetime.now(UTC)
        run = ExecutionRun(
            run_id=str(uuid4()),
            workspace_id=workspace_id,
            snapshot_hash=snapshot.semantic_hash,
            snapshot_root_hash=snapshot.root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=snapshot.target_hash,
            target_database=snapshot.target_database,
            batch_rows=DEFAULT_CREATE_BATCH_ROWS,
            status=ExecutionRunStatus.RUNNING,
            started_at=started_at,
            started_by=actor.identity.display_name,
            completed_at=None,
            rows=(),
        )
        self.journal.start_run(workspace_id, run, actor=actor, intent_assessment_hash=preview.business_intent_assessment_hash)
        return self.journal.finish_run(
            workspace_id,
            run.run_id,
            ExecutionRunStatus.COMPLETED,
            actor=actor,
        )


    def operator_error(self, workspace_id: str, run_id: str, row_id: str, *, actor: Actor) -> tuple[ExecutionRowAttempt, OperatorErrorReceipt | None]:
        """Read private submitted values only with protected workspace authority."""
        self.authorization.require(actor, Capability.PROJECT_VIEW, workspace_id=workspace_id)
        self.authorization.require(actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=workspace_id)
        self.workspaces.get(workspace_id)
        if isinstance(self.journal, OperatorErrorRepository):
            record = self.journal.operator_error_for_row(workspace_id, run_id, row_id)
            if record is None:
                raise WorkspaceError("The saved load record was not found")
            return record
        run = self.journal.get_run(workspace_id, run_id)
        row = next((item for item in run.rows if item.row_id == row_id), None) if run else None
        if row is None:
            raise WorkspaceError("The saved load record was not found")
        return row, None

    def operator_error_rows(self, workspace_id: str, run_id: str | None, *, after: int, actor: Actor) -> tuple[str, tuple[tuple[int, ExecutionRowAttempt], ...]] | None:
        """Authorize bounded saved rejection navigation for every load entry point."""
        self.authorization.require(actor, Capability.PROJECT_VIEW, workspace_id=workspace_id)
        self.authorization.require(actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=workspace_id)
        self.workspaces.get(workspace_id)
        if isinstance(self.journal, OperatorErrorPageRepository):
            return self.journal.operator_error_rows(workspace_id, run_id, after=after)
        return None


    @staticmethod
    def _validate_execution_scope(
        workspace_state: WorkspaceState,
        preview: ExecutionPreview,
        executor: OdooWriteExecutor,
    ) -> None:
        snapshot = preview.snapshot
        if workspace_state.odoo_connection_mode not in {
            OdooConnectionMode.LOCAL,
            OdooConnectionMode.REMOTE,
        }:
            raise WorkspaceError(
                "Configure the exact Odoo load target first",
                load_failure_code="LOAD_CONFIGURE_THE_EXACT_ODOO_LOAD_TARGET_FIRST",
            )
        if preview.current_run is not None:
            raise WorkspaceError(
                "This preview was already loaded. Compare with Odoo again first.",
                load_failure_code="LOAD_THIS_PREVIEW_WAS_ALREADY_LOADED_COMPARE_WITH_ODOO_AGAIN_FIRST",
            )
        if preview.scope_error:
            raise WorkspaceError(
                preview.scope_error,
                load_failure_code="LOAD_SCOPE_CHECK_FAILED",
            )
        if not preview.can_load:
            raise WorkspaceError(
                "Resolve every blocked or ambiguous row before loading",
                load_failure_code="LOAD_RESOLVE_EVERY_BLOCKED_OR_AMBIGUOUS_ROW_BEFORE_LOADING",
            )
        if executor.target_hash != snapshot.target_hash:
            raise WorkspaceError(
                "The load connection points to a different Odoo target",
                load_failure_code="LOAD_THE_LOAD_CONNECTION_POINTS_TO_A_DIFFERENT_ODOO_TARGET",
            )
        if executor.scope_hash != preview.api_scope.semantic_hash:
            raise WorkspaceError(
                "The writer is not bound to this reviewed load preview",
                load_failure_code="LOAD_THE_WRITER_IS_NOT_BOUND_TO_THIS_REVIEWED_LOAD_PREVIEW",
            )


def _validate_write_identity(
    preview: ExecutionPreview,
    identity: OdooWriteIdentity | None,
    credential_binding_hash: str,
    *,
    required: bool,
) -> None:
    """Bind a supplied write probe to the target and exact preview scope."""

    if identity is None:
        if required:
            raise WorkspaceError(
                "Probe the separate remote write credential before loading",
                load_failure_code="LOAD_PROBE_THE_SEPARATE_REMOTE_WRITE_CREDENTIAL_BEFORE_LOADING",
            )
        if credential_binding_hash:
            raise WorkspaceError(
                "The write credential has no principal evidence",
                load_failure_code="LOAD_THE_WRITE_CREDENTIAL_HAS_NO_PRINCIPAL_EVIDENCE",
            )
        return
    if not credential_binding_hash:
        raise WorkspaceError(
            "The write principal has no credential-generation binding",
            load_failure_code="LOAD_THE_WRITE_PRINCIPAL_HAS_NO_CREDENTIAL_GENERATION_BINDING",
        )
    expected_readable = tuple(item.model for item in preview.api_scope.models)
    expected_writable = tuple(
        item.model for item in preview.api_scope.models if item.write_fields
    )
    if identity.target_hash != preview.snapshot.target_hash:
        raise WorkspaceError(
            "The write principal belongs to a different Odoo target",
            load_failure_code="LOAD_THE_WRITE_PRINCIPAL_BELONGS_TO_A_DIFFERENT_ODOO_TARGET",
        )
    if (
        preview.snapshot.read_context_hash
        and identity.context_hash != preview.snapshot.read_context_hash
    ):
        raise WorkspaceError(
            "The write credential does not use the reviewed Odoo company context; "
            "check the Odoo access and compare again",
            load_failure_code="LOAD_THE_WRITE_CREDENTIAL_DOES_NOT_USE_THE_REVIEWED_ODOO_COMPANY_CONTEXT_CHECK_THE_ODOO_ACCESS_AND_COMPARE_AGAIN",
        )
    if identity.readable_models != expected_readable:
        raise WorkspaceError(
            "The write principal read-back scope changed",
            load_failure_code="LOAD_THE_WRITE_PRINCIPAL_READ_BACK_SCOPE_CHANGED",
        )
    if identity.writable_models != expected_writable:
        raise WorkspaceError(
            "The write principal model scope changed",
            load_failure_code="LOAD_THE_WRITE_PRINCIPAL_MODEL_SCOPE_CHANGED",
        )
    hashes = (
        credential_binding_hash,
        identity.principal_hash,
        identity.permission_hash,
        identity.context_hash,
    )
    if not all(_SHA256.fullmatch(value) for value in hashes):
        raise WorkspaceError(
            "Execution write-identity evidence is invalid",
            load_failure_code="LOAD_EXECUTION_WRITE_IDENTITY_EVIDENCE_IS_INVALID",
        )


def _require_resume_write_identity(
    run: ExecutionRun,
    identity: OdooWriteIdentity | None,
    credential_binding_hash: str,
    *,
    required: bool,
) -> None:
    """Require the original target principal before resumed writes."""

    expected = (
        run.write_principal_hash,
        run.write_permission_hash,
        run.write_context_hash,
    )
    if not any(expected):
        if required:
            raise WorkspaceError(
                "The interrupted load has no approved remote write identity",
                load_failure_code="LOAD_THE_INTERRUPTED_LOAD_HAS_NO_APPROVED_REMOTE_WRITE_IDENTITY",
            )
        if identity is not None or credential_binding_hash:
            raise WorkspaceError(
                "The local load cannot acquire a remote write identity during recovery",
                load_failure_code="LOAD_THE_LOCAL_LOAD_CANNOT_ACQUIRE_A_REMOTE_WRITE_IDENTITY_DURING_RECOVERY",
            )
        return
    if identity is None:
        raise WorkspaceError(
            "Re-probe the approved write principal before resuming",
            load_failure_code="LOAD_RE_PROBE_THE_APPROVED_WRITE_PRINCIPAL_BEFORE_RESUMING",
        )
    actual = (
        identity.principal_hash,
        identity.permission_hash,
        identity.context_hash,
    )
    if (
        actual != expected
        or identity.target_hash != run.target_hash
        or not _SHA256.fullmatch(credential_binding_hash)
    ):
        raise WorkspaceError(
            "The write principal, permission, or context changed after interruption",
            load_failure_code="LOAD_THE_WRITE_PRINCIPAL_PERMISSION_OR_CONTEXT_CHANGED_AFTER_INTERRUPTION",
        )


def _validate_read_identity(
    preview: ExecutionPreview,
    identity: OdooReadIdentity | None,
    credential_binding_hash: str,
    *,
    required: bool,
) -> None:
    """Re-probe the comparison principal before any remote write is journalled."""

    snapshot = preview.snapshot
    if identity is None:
        if required:
            raise WorkspaceError(
                "Re-probe the current Odoo read key before loading",
                load_failure_code="LOAD_RE_PROBE_THE_CURRENT_ODOO_READ_KEY_BEFORE_LOADING",
            )
        if credential_binding_hash:
            raise WorkspaceError(
                "The read credential has no principal evidence",
                load_failure_code="LOAD_THE_READ_CREDENTIAL_HAS_NO_PRINCIPAL_EVIDENCE",
            )
        return
    expected = (
        snapshot.read_credential_binding_hash,
        snapshot.read_principal_hash,
        snapshot.read_permission_hash,
        snapshot.read_context_hash,
    )
    actual = (
        credential_binding_hash,
        identity.principal_hash,
        identity.permission_hash,
        identity.context_hash,
    )
    if not all(_SHA256.fullmatch(value) for value in (*expected, *actual)):
        raise WorkspaceError(
            "Execution read-identity evidence is incomplete",
            load_failure_code="LOAD_EXECUTION_READ_IDENTITY_EVIDENCE_IS_INCOMPLETE",
        )
    if (
        actual != expected
        or identity.target_hash != snapshot.target_hash
        or identity.readable_models != snapshot.readable_models
    ):
        raise WorkspaceError(
            "The Odoo read key, principal, permissions, or context changed; "
            "refresh the schema and compare again",
            load_failure_code="LOAD_THE_ODOO_READ_KEY_PRINCIPAL_PERMISSIONS_OR_CONTEXT_CHANGED_REFRESH_THE_SCHEMA_AND_COMPARE_AGAIN",
        )


def _read_credential_snapshot_error(
    workspace_state: WorkspaceState,
    snapshot: ExecutionSnapshot,
    *,
    current_read_credential_binding: str | None,
) -> str:
    """Explain only credential-dependent refresh failures for focused UI recovery."""

    if (
        workspace_state.odoo_connection_mode is not OdooConnectionMode.REMOTE
        or current_read_credential_binding is None
    ):
        return ""
    evidence = (
        snapshot.read_credential_binding_hash,
        snapshot.read_principal_hash,
        snapshot.read_permission_hash,
        snapshot.read_context_hash,
    )
    if not all(_SHA256.fullmatch(value) for value in evidence):
        return "Refresh the remote Odoo schema and compare again"
    if not current_read_credential_binding:
        return (
            "Enter the current Odoo read key, refresh the schema, and compare again"
        )
    if current_read_credential_binding != snapshot.read_credential_binding_hash:
        return "The Odoo read key changed; refresh the schema and compare again"
    return ""
