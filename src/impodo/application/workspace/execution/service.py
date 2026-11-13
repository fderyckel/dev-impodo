"""Orchestrate one confirmed schema-bound load from the current snapshot."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from impodo.application.workspace.execution.navigation import (
    ExecutionNavigationState,
    ExecutionReviewProjection,
)
from impodo.domain.execution.dependency_scheduler import dependency_component_pages
from impodo.domain.execution.load_control import (
    LoadControl,
    LoadControlState,
    LoadHalted,
    RejectionPolicy,
    can_set_aside,
)
from impodo.domain.execution.models import (
    MAX_CREATE_BATCH_ROWS,
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
    ProjectedOdooReceipt,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    MAX_IDENTITY_LOOKUP_KEYS,
    MAX_PROJECTED_RECEIPT_IDS,
    OdooIdentityLookupUnavailable,
    OdooWriteError,
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    dependency_ordered_execution_datasets,
)
from impodo.domain.mapping.source_record_identity import SOURCE_EXTERNAL_IDS_FIELD
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.reconciliation import (
    ReconciliationRowStatus,
    ReconciliationRun,
)
from impodo.domain.shared.access import (
    Actor,
    Capability,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    OdooReadIdentity,
    OdooWriteIdentity,
    canonical_json_text,
    portable_value,
    target_identity_hash,
    target_record_binding_hash,
)
from impodo.domain.target_numeric_precision import TargetNumericPrecisionLosses
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
from .identity_lookups import NestedIdentityLookup
from .load_observation import (
    current_load_observation,
    emit_load_evidence,
    observe_journal_batch,
    observe_load_context,
    observe_load_journal,
    observe_load_local_phase,
    observe_load_transport,
    record_load_failure,
)
from .load_transport import (
    DependentCompletions,
    LoadControlRepository,
    create_transport_results,
)

DEFAULT_CREATE_BATCH_ROWS = 10
NO_WRITE_ROWS_MESSAGE = "This preview has no rows to create or update"
MAX_VISIBLE_LOAD_GROUPS = 5
MAX_VISIBLE_GROUP_DATASETS = 3
MAX_VISIBLE_BLOCKER_GROUPS = 5
MAX_VISIBLE_BLOCKER_DATASETS = 3
MAX_PROGRESS_ROWS_BETWEEN_UPDATES = 25
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ReadCredentialBindingProvider = Callable[[WorkspaceState, Actor], str]


def _targeted_recovery_row_ids(
    snapshot: ExecutionSnapshot,
    run: ExecutionRun,
) -> frozenset[str]:
    """Keep all uncertain rows and the committed dependency frontier.

    An omitted committed row retains its journal receipt. It is read again by
    final reconciliation, before the load can be reported as verified.
    """

    snapshot_row_ids = {row.row_id for row in snapshot.rows}
    rows = {
        row.row_id: row
        for row in snapshot.rows
        if row.disposition in {"CREATE", "UPDATE"}
    }
    attempts = {item.row_id: item for item in run.rows}
    if set(rows) != set(attempts):
        raise WorkspaceError(
            "The saved load rows do not match the preview",
            load_failure_code="LOAD_THE_SAVED_LOAD_ROWS_DO_NOT_MATCH_THE_PREVIEW",
        )
    for item in attempts.values():
        if item.status is ExecutionRowStatus.COMMITTED and item.odoo_id is None:
            raise WorkspaceError(
                "A committed load row has no Odoo record",
                load_failure_code="LOAD_A_COMMITTED_LOAD_ROW_HAS_NO_ODOO_RECORD",
            )

    by_model: dict[str, set[str]] = {}
    by_dataset: dict[str, set[str]] = {}
    for row_id, row in rows.items():
        if attempts[row_id].status is ExecutionRowStatus.COMMITTED:
            by_model.setdefault(row.target_model, set()).add(row_id)
            by_dataset.setdefault(row.dataset, set()).add(row_id)
    dataset_parents = {
        item.dataset: item.dependencies for item in snapshot.datasets
    }
    completion_dependencies: dict[str, set[str]] = {}
    for item in snapshot.relationship_plan.completions:
        completion_dependencies.setdefault(item.row_id, set()).update(
            item.dependency_row_ids
        )

    # PLANNED rows were never sent to Odoo, and RETRY_READY rows were already
    # proved absent by an earlier recovery. Use both to discover the committed
    # dependency frontier without manufacturing read-back outcomes for tens of
    # thousands of records that have never been attempted.
    selected = {
        row_id
        for row_id, item in attempts.items()
        if item.status
        not in {
            ExecutionRowStatus.COMMITTED,
            ExecutionRowStatus.PLANNED,
            ExecutionRowStatus.RETRY_READY,
        }
    }
    frontier = {
        row_id
        for row_id, item in attempts.items()
        if item.status is not ExecutionRowStatus.COMMITTED
    }
    pending = list(frontier)
    visited = set(frontier)
    expanded_models: set[str] = set()
    expanded_datasets: set[str] = set()
    while pending:
        row_id = pending.pop()
        row = rows[row_id]
        related_ids = set(completion_dependencies.get(row_id, ()))
        related_models: set[str] = set()
        related_datasets = set(dataset_parents.get(row.dataset, ()))
        for intent in row.fields:
            related_ids.update(intent.dependency_row_ids)
            if intent.kind == "relation" and intent.action == "SET_VALUE":
                related_models.add(intent.related_model)
                values = intent.value if isinstance(intent.value, tuple) else (intent.value,)
                for value in values:
                    if (
                        isinstance(value, LogicalReference)
                        and value.origin == "incoming"
                        and value.dataset
                    ):
                        related_datasets.add(value.dataset)
        for value in (*row.business_identity, *row.business_scope):
            if isinstance(value, BusinessReference) and value.model:
                related_models.add(value.model)
            elif (
                isinstance(value, LogicalReference)
                and value.origin == "incoming"
                and value.dataset
            ):
                related_datasets.add(value.dataset)
        for model in related_models.difference(expanded_models):
            related_ids.update(by_model.get(model, ()))
            expanded_models.add(model)
        for dataset in related_datasets.difference(expanded_datasets):
            related_ids.update(by_dataset.get(dataset, ()))
            expanded_datasets.add(dataset)
        unknown_ids = related_ids.difference(snapshot_row_ids)
        if unknown_ids:
            raise WorkspaceError(
                "A recovery dependency is missing from the preview",
                load_failure_code="LOAD_A_RECOVERY_DEPENDENCY_IS_MISSING_FROM_THE_PREVIEW",
            )
        new_ids = related_ids.intersection(rows).difference(visited)
        visited.update(new_ids)
        pending.extend(new_ids)
        selected.update(
            item
            for item in new_ids
            if attempts[item].status is ExecutionRowStatus.COMMITTED
        )
    return frozenset(selected)


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


class ExecutionJournalRepository(Protocol):
    def close_interrupted_run_for_recomparison(
        self, workspace_id: str, expected_run: ExecutionRun,
        expected_preflight_run_id: str, *, actor: Actor,
    ) -> ExecutionRun:
        """Close an interrupted run after ``ExecutionService`` verifies recheck evidence."""
        ...

    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        transfer_preflight_hash: str = "",
        intent_assessment_hash: str = "",
        load_control: LoadControl | None = None,
    ) -> None:
        """Durably start the run that ``ExecutionService`` has authorized."""
        ...

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Persist terminal row outcomes reported by the Odoo write executor."""
        ...

    def record_batch_started(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Persist the uncertain batch boundary before a remote Odoo write."""
        ...

    def record_recovery(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        actor: Actor,
    ) -> None:
        """Persist read-back recovery outcomes before an interrupted run resumes."""
        ...

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        """Close the journal after the execution service reaches a final outcome."""
        ...

    def get_current_run(
        self,
        workspace_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None:
        """Return the current run, optionally only when its snapshot still matches."""
        ...

    def get_run(self, workspace_id: str, run_id: str) -> ExecutionRun | None:
        """Return one run for execution, recovery, and reconciliation services."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutionDatasetPreview:
    dataset: str
    target_model: str
    create_count: int
    update_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class ExecutionLoadGroupPreview:
    """Bounded business-language projection of one frozen schedule layer."""

    number: int
    record_count: int
    dataset_labels: tuple[str, ...]
    omitted_dataset_count: int


@dataclass(frozen=True, slots=True)
class ExecutionDependencySummary:
    """Compact load-order guidance derived from the immutable snapshot."""

    groups: tuple[ExecutionLoadGroupPreview, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0
    relationship_record_count: int = 0
    relationship_field_count: int = 0
    relationship_link_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionBlockerGroup:
    """One actionable browser message for equivalent blocked rows."""

    code: str
    title: str
    action: str
    record_count: int
    dataset_labels: tuple[str, ...] = ()
    omitted_dataset_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionBlockerSummary:
    """Bounded blocker categories without exposing source values or row IDs."""

    groups: tuple[ExecutionBlockerGroup, ...] = ()
    total_group_count: int = 0
    omitted_group_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionPreview:
    snapshot: ExecutionSnapshot
    datasets: tuple[ExecutionDatasetPreview, ...]
    current_run: ExecutionRun | None
    api_scope: OdooApiScope
    deferred_create_count: int
    scope_error: str = ""
    scope_error_code: str = ""
    credential_refresh_required: bool = False
    dependency_summary: ExecutionDependencySummary = field(
        default_factory=ExecutionDependencySummary
    )
    blocker_summary: ExecutionBlockerSummary = field(
        default_factory=ExecutionBlockerSummary
    )
    prior_interrupted_run: ExecutionRun | None = None
    business_intent_assessment_hash: str = ""
    business_intent: BusinessIntentReview | None = None

    @property
    def can_load(self) -> bool:
        """Say whether the reviewed snapshot is eligible to begin a load.

        Used by web confirmation routes and ``ExecutionService.execute`` as a
        pre-check. The service repeats full identity and snapshot validation
        before starting the durable journal.
        """
        return (
            self.snapshot.write_count > 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.current_run is None
            and not self.scope_error
        )

    @property
    def can_complete_without_load(self) -> bool:
        """Say whether a no-write snapshot may be recorded as complete.

        Used by the completion route and ``complete_no_changes``. It excludes
        blocked, ambiguous, or stale-target evidence even though no Odoo write
        is planned.
        """
        return (
            self.snapshot.write_count == 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.scope_error in {"", NO_WRITE_ROWS_MESSAGE}
            and (
                self.current_run is None
                or (
                    self.current_run.status is ExecutionRunStatus.COMPLETED
                    and self.current_run.total_count == 0
                )
            )
        )


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


@dataclass(frozen=True, slots=True)
class ExecutionNavigationPreview:
    """Bounded load-stage state that never materializes execution rows."""

    state: ExecutionNavigationState
    scope_error: str = ""
    credential_refresh_required: bool = False

    @property
    def write_count(self) -> int:
        """Expose the scalar write total supplied by navigation state."""
        return self.state.summary.write_count

    @property
    def current_run_id(self) -> str:
        """Expose the current durable run identifier for navigation controls."""
        return self.state.execution_run_id

    @property
    def current_run(self) -> str | None:
        """Expose the current run truthiness used by the review page."""

        return self.current_run_id or None

    @property
    def can_load(self) -> bool:
        """Return the navigation-level eligibility of the Load action.

        ``WorkspaceNavigationQueryService`` consumes this compact result. The
        full ``ExecutionPreview.can_load`` remains the command-time authority.
        """
        return bool(
            self.write_count
            and not self.current_run_id
            and not self.scope_error
            and not self.state.summary.has_attention
        )

    @property
    def can_complete_without_load(self) -> bool:
        """Offer the zero-write review without decoding execution rows."""
        return bool(
            not self.write_count
            and not self.current_run_id
            and not self.scope_error
            and not self.state.summary.has_attention
        )


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
    ) -> None:
        self.workspaces = workspaces
        self.preflight = preflight
        self.journal = journal
        self.authorization = authorization
        self.require_remote_read_identity = require_remote_read_identity
        self.require_remote_write_identity = require_remote_write_identity
        self.current_read_credential_binding = current_read_credential_binding
        self.load_controls = load_controls

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
        dataset_counts: dict[str, Counter[str]] = {}
        for row in snapshot.rows:
            dataset_counts.setdefault(row.dataset, Counter())[row.disposition] += 1
        snapshot_error = _execution_snapshot_error(workspace_state, snapshot)
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
        return ExecutionPreview(
            snapshot=snapshot,
            datasets=tuple(
                ExecutionDatasetPreview(
                    dataset=dataset.dataset,
                    target_model=dataset.target_model,
                    create_count=dataset_counts[dataset.dataset]["CREATE"],
                    update_count=dataset_counts[dataset.dataset]["UPDATE"],
                    unchanged_count=dataset_counts[dataset.dataset]["UNCHANGED"],
                )
                for dataset in snapshot.datasets
                if dataset.dataset in dataset_counts
            ),
            current_run=current,
            api_scope=api_scope,
            deferred_create_count=_planned_deferred_create_count(
                snapshot,
                create_batch_rows=DEFAULT_CREATE_BATCH_ROWS,
            ),
            scope_error=scope_error,
            scope_error_code=_execution_scope_error_code(scope_error),
            credential_refresh_required=bool(credential_error),
            dependency_summary=_execution_dependency_summary(snapshot),
            blocker_summary=_execution_blocker_summary(snapshot),
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
            code=_execution_scope_error_code(scope_error),
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
                    _portable_key(row.source_identity), _portable_key(row.business_identity),
                    _portable_key(row.business_scope))
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
        progress: Callable[[ExecutionRun], None] | None = None,
        expected_intent_assessment_hash: str | None = None,
        rejection_policy: RejectionPolicy = RejectionPolicy.STOP,
    ) -> ExecutionRun:
        """Start and carry out a reviewed conventional Odoo load.

        Called by the confirmed-load worker after its browser job is queued.
        It revalidates the immutable preflight snapshot, target identity,
        credentials, and authority before journaling any remote write; the
        lower-level ``_continue_run`` owns batch transport and receipts.
        """
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

        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = self._resolve_identity_crosswalk(
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
            self.journal.start_run(workspace_id, run, actor=actor, intent_assessment_hash=preview.business_intent_assessment_hash, load_control=control)
            run = replace(run, load_control=control)
        else:
            if rejection_policy is not RejectionPolicy.STOP:
                raise WorkspaceError(
                    "Load continuation is unavailable for this journal",
                    load_failure_code="LOAD_LOAD_CONTINUATION_IS_UNAVAILABLE_FOR_THIS_JOURNAL",
                )
            self.journal.start_run(workspace_id, run, actor=actor, intent_assessment_hash=preview.business_intent_assessment_hash)
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
        progress: Callable[[ExecutionRun], None] | None = None,
    ) -> ExecutionRun:
        """Start a governed Odoo-to-Odoo transfer from a qualified source.

        Called by the transfer-load worker. It shares the journal and write
        safeguards of ``execute`` while additionally validates the frozen
        source-finalization and transfer-preflight evidence.
        """
        """Enter the shared writer from a current, confirmed transfer snapshot."""

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
            deferred_create_count=_planned_deferred_create_count(
                snapshot,
                create_batch_rows=create_batch_rows,
            ),
            scope_error=_execution_snapshot_error(destination_workspace, snapshot),
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
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        self._assert_create_rows_still_absent(write_rows, metadata, executor)
        identity_cache = self._resolve_identity_crosswalk(
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
        self.journal.start_run(
            workspace_id,
            run,
            actor=actor,
            transfer_preflight_hash=expected_preflight_hash,
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
        progress: Callable[[ExecutionRun], None] | None = None,
        expected_load_control_hash: str = "",
    ) -> ExecutionRun:
        """Recover an interrupted conventional load before attempting retries.

        Called by the confirmed-load worker for an active interrupted journal.
        It delegates read-back classification to reconciliation, persists that
        recovery, and only then calls ``_continue_run`` for retry-safe rows.
        """
        """Resume an interrupted journal only from exact read-back evidence."""

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
        recovery = ReconciliationRun.from_json(recovery.to_json())
        recovery_hash = recovery.semantic_hash
        already_applied = (
            not run.in_flight_count
            and all(
                item.recovery_hash == recovery_hash
                for item in run.rows
            )
        )
        if not already_applied:
            recovered = self._classify_recovery(
                snapshot, run, recovery, targeted=True,
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

        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = self._resolve_identity_crosswalk(
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
        classified = self._classify_recovery(
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
        progress: Callable[[ExecutionRun], None] | None = None,
    ) -> ExecutionRun:
        """Recover an interrupted Odoo-to-Odoo transfer after source rechecks.

        Called by the transfer-load worker. It mirrors ``resume`` but preserves
        transfer-specific source evidence and external-ID create semantics.
        """
        """Resume one interrupted transfer from exact same-key read-back."""

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
            scope_error=_execution_snapshot_error(
                destination_workspace, snapshot, operation=OdooOperation.RECOVER,
            ),
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

        recovery = ReconciliationRun.from_json(recovery.to_json())
        recovery_hash = recovery.semantic_hash
        already_applied = (
            not run.in_flight_count
            and all(item.recovery_hash == recovery_hash for item in run.rows)
        )
        recovered = (
            run.rows
            if already_applied
            else self._classify_recovery(snapshot, run, recovery)
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
        self._assert_create_rows_still_absent(
            retryable_creates,
            metadata,
            executor,
        )
        if not already_applied:
            self.journal.record_recovery(
                workspace_id,
                run.run_id,
                recovered,
                actor=actor,
            )
            run = self.journal.get_run(workspace_id, run.run_id)
            if run is None:
                raise WorkspaceError(
                    "The recovered transfer journal could not be reloaded",
                    load_failure_code="LOAD_THE_RECOVERED_TRANSFER_JOURNAL_COULD_NOT_BE_RELOADED",
                )

        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache = self._resolve_identity_crosswalk(
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
                self._record_outcomes(workspace_id, run_id, pending)
            self.journal.finish_run(workspace_id, run_id, ExecutionRunStatus.COMPLETED_WITH_ERRORS, actor=actor)
        return control

    def _continue_run(
        self, workspace_state: WorkspaceState, snapshot: ExecutionSnapshot,
        run: ExecutionRun, executor: OdooWriteExecutor, actor: Actor, *,
        identity_cache: dict[str, int], progress: Callable[[ExecutionRun], None] | None,
        protected_values: Mapping[str, Any] | None = None, create_with_external_ids: bool = False,
    ) -> ExecutionRun:
        """End new transport at a durable pause/stop while retaining all receipts."""
        observe_load_journal(run, projection="INITIAL")
        try:
            return self._transport_run(workspace_state, snapshot, run, executor, actor,
                identity_cache=identity_cache, progress=progress, protected_values=protected_values,
                create_with_external_ids=create_with_external_ids)
        except (LoadHalted, WorkspaceError) as error:
            saved = self.journal.get_run(run.workspace_id, run.run_id)
            if isinstance(error, WorkspaceError) and not (
                saved is not None and saved.load_control is not None
                and saved.load_control.state is LoadControlState.STOPPED
                and saved.status is ExecutionRunStatus.COMPLETED_WITH_ERRORS
                and not saved.in_flight_count
            ):
                raise
            if saved is None or saved.load_control is None or saved.in_flight_count:
                raise WorkspaceError(
                    "The halted load needs read-back before continuation",
                    load_failure_code="LOAD_THE_HALTED_LOAD_NEEDS_READ_BACK_BEFORE_CONTINUATION",
                ) from None
            if saved.status is not ExecutionRunStatus.RUNNING:
                if progress is not None:
                    progress(saved)
                return saved
            if saved.load_control.state is LoadControlState.STOP_REQUESTED:
                recorded = {row.row_id: row for row in saved.rows}
                pending = tuple(row for row in snapshot.rows if row.row_id in recorded
                    and recorded[row.row_id].status in {ExecutionRowStatus.PLANNED, ExecutionRowStatus.RETRY_READY})
                self._record_blocked(run.workspace_id, run.run_id, pending, recorded,
                    "Not attempted because the operator stopped further uploads")
                saved = self.journal.finish_run(run.workspace_id, run.run_id,
                    ExecutionRunStatus.COMPLETED_WITH_ERRORS, actor=actor)
            if progress is not None:
                progress(saved)
            return saved
        finally:
            if current_load_observation() is not None:
                try:
                    observed_run = self.journal.get_run(run.workspace_id, run.run_id)
                except Exception as capture_error:  # noqa: BLE001 - optional evidence read
                    emit_load_evidence("capture_failed", exception_class=type(capture_error).__name__)
                else:
                    if observed_run is not None:
                        observe_load_journal(observed_run, projection="FINAL")
                    else:
                        emit_load_evidence("capture_failed", reason="JOURNAL_UNAVAILABLE")

    def _transport_run(
        self,
        workspace_state: WorkspaceState,
        snapshot: ExecutionSnapshot,
        run: ExecutionRun,
        executor: OdooWriteExecutor,
        actor: Actor,
        *,
        identity_cache: dict[str, int],
        progress: Callable[[ExecutionRun], None] | None,
        protected_values: Mapping[str, Any] | None = None,
        create_with_external_ids: bool = False,
    ) -> ExecutionRun:
        """Consume unfinished rows from one durable schedule."""

        create_with_external_ids = create_with_external_ids or any(SOURCE_EXTERNAL_IDS_FIELD in item.identity_fields for item in snapshot.datasets)
        workspace_id = workspace_state.workspace_id
        write_rows = tuple(
            row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        create_batch_rows = validated_create_batch_rows(run.batch_rows or 0)
        progress_sink = progress
        if progress_sink is not None:
            with observe_load_local_phase("progress_publication"):
                progress_sink(run)

        recorded: dict[str, ExecutionRowAttempt] = {
            item.row_id: item for item in run.rows
        }
        accepted_models = {item.target_model for item in run.rows if item.status in {ExecutionRowStatus.COMMITTED, ExecutionRowStatus.PARTIALLY_APPLIED}}
        continuation_policy = bool(run.load_control and run.load_control.policy is RejectionPolicy.CONTINUE)
        if continuation_policy and run.load_control is not None and not run.load_control.windows:
            accepted_models.clear()
        progress_rows_since_publish = 0
        progress_row_interval = min(
            create_batch_rows,
            MAX_PROGRESS_ROWS_BETWEEN_UPDATES,
        )

        def publish_progress(
            *,
            changed_rows: int = 0,
            force: bool = False,
        ) -> None:
            """Publish bounded UI progress without rebuilding the run per row."""

            nonlocal progress_rows_since_publish
            if progress_sink is None:
                return
            progress_rows_since_publish += max(0, int(changed_rows))
            if not force and progress_rows_since_publish < progress_row_interval:
                return
            with observe_load_local_phase("progress_publication"):
                progress_sink(replace(run, rows=tuple(recorded.values())))
            progress_rows_since_publish = 0

        source_cache: dict[tuple[str, str, str, str], int] = {
            (
                row.dataset,
                _portable_key(row.source_identity),
                row.target_model,
                "",
            ): attempt.odoo_id
            for row in write_rows
            if row.disposition == "CREATE"
            and (attempt := recorded[row.row_id]).odoo_id is not None
        }
        for row in write_rows:
            attempt = recorded[row.row_id]
            for receipt in attempt.projected_receipts:
                source_cache[
                    (
                        row.dataset,
                        _portable_key(row.source_identity),
                        receipt.target_model,
                        receipt.projection_field,
                    )
                ] = receipt.odoo_id
        for row in write_rows:
            attempt = recorded[row.row_id]
            if attempt.odoo_id is not None:
                identity_cache[_identity_cache_key(row)] = attempt.odoo_id
        deferred_by_row: dict[str, tuple[FieldIntent, ...]] = {
            row.row_id: deferred
            for row in write_rows
            if recorded[row.row_id].status is ExecutionRowStatus.PARTIALLY_APPLIED
            and (deferred := self._deferred_create_intents(row))
        }
        projected_requirements = _projected_receipt_requirements(snapshot)
        stop_after_unknown = False
        stop_after_rejection = False
        next_transport_batch = 1 + max(
            (item.transport_batch for item in run.rows),
            default=-1,
        )
        row_by_id = {row.row_id: row for row in snapshot.rows}
        component_pages = dependency_component_pages(
            
                (component.sequence, component.row_ids)
                for component in snapshot.relationship_plan.components
            
        )
        scheduled_groups = (
            (page, dataset, dataset_rows)
            for page in component_pages
            for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence)
            if (
                dataset_rows := tuple(
                    row_by_id[row_id]
                    for row_id in page.row_ids
                    if row_by_id[row_id].dataset == dataset.dataset
                )
            )
        )
        for page, dataset, dataset_rows in scheduled_groups:
            if stop_after_unknown or stop_after_rejection:
                self._record_blocked(
                    workspace_id,
                    run.run_id,
                    dataset_rows,
                    recorded,
                    (
                        "Not attempted after an uncertain Odoo response"
                        if stop_after_unknown
                        else "Not attempted after an Odoo rejection"
                    ),
                )
                publish_progress(changed_rows=len(dataset_rows))
                continue
            try:
                self._ensure_projected_receipts(
                    workspace_id,
                    run.run_id,
                    dataset_rows,
                    row_by_id,
                    recorded,
                    metadata,
                    source_cache,
                    identity_cache,
                    executor,
                    projected_requirements,
                    deferred_by_row,
                )
            except (WorkspaceError, OdooWriteRejected) as error:
                self._record_blocked(
                    workspace_id,
                    run.run_id,
                    dataset_rows,
                    recorded,
                    str(error),
                )
                publish_progress(changed_rows=len(dataset_rows))
                continue
            creates = tuple(
                row
                for row in dataset_rows
                if row.disposition == "CREATE"
                and recorded[row.row_id].status
                in {
                    ExecutionRowStatus.PLANNED,
                    ExecutionRowStatus.RETRY_READY,
                }
            )
            updates = tuple(
                row
                for row in dataset_rows
                if row.disposition == "UPDATE"
                and recorded[row.row_id].status
                in {
                    ExecutionRowStatus.PLANNED,
                    ExecutionRowStatus.RETRY_READY,
                }
            )
            if (
                workspace_state.source_mode is SourceMode.ODOO
                and creates
                and any(
                    attempt.status is ExecutionRowStatus.COMMITTED
                    for attempt in recorded.values()
                )
            ):
                # A prior wave can cause Odoo to generate records that were
                # absent at the pre-journal check. Never import those children
                # as fresh rows from an already approved snapshot.
                try:
                    self._assert_create_rows_still_absent(creates, metadata, executor)
                except WorkspaceError as error:
                    self._record_blocked(
                        workspace_id,
                        run.run_id,
                        dataset_rows,
                        recorded,
                        str(error),
                    )
                    publish_progress(changed_rows=len(dataset_rows))
                    stop_after_rejection = True
                    continue
            start = 0
            while start < len(creates):
                group_size = min(create_batch_rows, 10) if continuation_policy and dataset.target_model not in accepted_models else create_batch_rows
                batch = creates[start : start + group_size]
                start += len(batch)
                prepared_rows: list[
                    tuple[ExecutionRow, dict[str, Any], tuple[FieldIntent, ...]]
                ] = []
                for row in batch:
                    try:
                        self._require_dependency_receipts(
                            row,
                            row_by_id,
                            recorded,
                        )
                        deferred = self._deferred_create_intents(row)
                        values = self._row_values(
                            row,
                            metadata,
                            by_source,
                            source_cache,
                            identity_cache,
                            executor,
                            protected_values=protected_values,
                            import_relations=(
                                create_with_external_ids
                                or SOURCE_EXTERNAL_IDS_FIELD in metadata[row.dataset].identity_fields
                                or workspace_state.odoo_connection_mode
                                is OdooConnectionMode.REMOTE
                            ),
                            skip_fields=frozenset(
                                intent.field for intent in deferred
                            ),
                        )
                    except (WorkspaceError, OdooWriteRejected) as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.BLOCKED,
                            safe_error=str(error),
                        )
                        self._record_outcomes(
                            workspace_id, run.run_id, (outcome,)
                        )
                        recorded[row.row_id] = outcome
                        publish_progress(changed_rows=1)
                    else:
                        prepared_rows.append((row, values, deferred))
                if not prepared_rows:
                    continue
                groups: dict[
                    tuple[str, ...],
                    list[
                        tuple[
                            ExecutionRow,
                            dict[str, Any],
                            tuple[FieldIntent, ...],
                        ]
                    ],
                ] = {}
                for prepared in prepared_rows:
                    groups.setdefault(tuple(sorted(prepared[1])), []).append(prepared)
                for prepared_group in groups.values():
                    if stop_after_unknown or stop_after_rejection:
                        break
                    import_create = (
                        create_with_external_ids
                        or workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
                        or any(SOURCE_EXTERNAL_IDS_FIELD in metadata[item[0].dataset].identity_fields for item in prepared_group)
                    )
                    continuation = bool(run.load_control and run.load_control.policy is RejectionPolicy.CONTINUE)

                    def begin(group_rows: tuple[ExecutionRow, ...], *, component: int = page.component_sequence, page_sequence: int = page.page_sequence) -> None:
                        nonlocal next_transport_batch
                        self._start_transport_batch(workspace_id, run.run_id, group_rows, recorded,
                            phase="CREATE", component=component,
                            page=page_sequence, batch=next_transport_batch)
                        next_transport_batch += 1

                    def rolled_back(group_rows: tuple[ExecutionRow, ...], error: OdooWriteRejected) -> None:
                        if self.load_controls is None:
                            raise WorkspaceError(
                                "Load isolation has no durable journal",
                                load_failure_code="LOAD_LOAD_ISOLATION_HAS_NO_DURABLE_JOURNAL",
                            )
                        retries = self.load_controls.record_rejected_batch(
                            workspace_id, run.run_id, tuple(recorded[row.row_id] for row in group_rows), error)
                        recorded.update({item.row_id: item for item in retries})

                    for result in create_transport_results(prepared_group, model=dataset.target_model,
                            executor=executor, import_create=import_create, continuation=continuation,
                            begin=begin, rolled_back=rolled_back):
                        error = result.error
                        if error is not None:
                            unknown = isinstance(error, OdooWriteOutcomeUnknown)
                            outcomes = tuple(replace(recorded[row.row_id],
                                status=ExecutionRowStatus.OUTCOME_UNKNOWN if unknown else ExecutionRowStatus.FAILED,
                                safe_error=str(error), diagnostic=_write_diagnostic(error, row, len(result.prepared),
                                    method="load" if import_create else "create"))
                                for row, _values, _deferred in result.prepared)
                            self._record_outcomes(workspace_id, run.run_id, outcomes)
                            recorded.update({item.row_id: item for item in outcomes})
                            publish_progress(changed_rows=len(outcomes), force=True)
                            if unknown:
                                stop_after_unknown = True
                            elif not (continuation and isinstance(error, OdooWriteRejected) and can_set_aside(error)):
                                stop_after_rejection = True
                            continue
                        accepted_models.add(dataset.target_model)
                        outcomes = []
                        for (row, _values, deferred), identifier in zip(
                            result.prepared, result.identifiers, strict=True
                        ):
                            needs_projection = bool(
                                projected_requirements.get(row.row_id)
                            )
                            outcome = replace(
                                recorded[row.row_id],
                                status=(
                                    ExecutionRowStatus.PARTIALLY_APPLIED
                                    if deferred or needs_projection
                                    else ExecutionRowStatus.COMMITTED
                                ),
                                odoo_id=identifier,
                                safe_error=(
                                    "Created; deferred relationship update pending"
                                    if deferred
                                    else ""
                                ),
                            )
                            if needs_projection and not deferred:
                                outcome = replace(
                                    outcome,
                                    safe_error=(
                                        "Created; generated relationship read-back pending"
                                    ),
                                )
                            outcomes.append(outcome)
                            if deferred:
                                deferred_by_row[row.row_id] = deferred
                            source_cache[
                                (
                                    row.dataset,
                                    _portable_key(row.source_identity),
                                    row.target_model,
                                    "",
                                )
                            ] = identifier
                            identity_cache[_identity_cache_key(row)] = identifier
                        self._record_outcomes(
                            workspace_id, run.run_id, outcomes
                        )
                        recorded.update({item.row_id: item for item in outcomes})
                        publish_progress(changed_rows=len(outcomes))
                if stop_after_unknown or stop_after_rejection:
                    break

            for row in updates:
                if stop_after_unknown or stop_after_rejection:
                    self._record_blocked(
                        workspace_id,
                        run.run_id,
                        (row,),
                        recorded,
                        (
                            "Not attempted after an uncertain Odoo response"
                            if stop_after_unknown
                            else "Not attempted after an Odoo rejection"
                        ),
                    )
                    publish_progress(changed_rows=1)
                    continue
                try:
                    self._require_dependency_receipts(
                        row,
                        row_by_id,
                        recorded,
                    )
                    record_id = self._find_row_id(
                        row,
                        metadata[row.dataset],
                        identity_cache,
                        executor,
                    )
                    values = self._row_values(
                        row,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                        protected_values=protected_values,
                    )
                except (WorkspaceError, OdooWriteRejected) as error:
                    outcome = replace(
                        recorded[row.row_id],
                        status=ExecutionRowStatus.BLOCKED,
                        safe_error=str(error),
                    )
                else:
                    self._start_transport_batch(
                        workspace_id,
                        run.run_id,
                        (row,),
                        recorded,
                        phase="UPDATE",
                        component=page.component_sequence,
                        page=page.page_sequence,
                        batch=next_transport_batch,
                        known_ids={row.row_id: record_id},
                    )
                    next_transport_batch += 1
                    try:
                        with observe_load_transport(row.target_model, "write", tuple(values), 1):
                            executor.update_row(row.target_model, record_id, values)
                    except OdooWriteOutcomeUnknown as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            safe_error=str(error),
                            diagnostic=_write_diagnostic(error, row),
                        )
                        stop_after_unknown = True
                    except OdooWriteRejected as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            safe_error=str(error),
                            diagnostic=_write_diagnostic(error, row),
                        )
                        stop_after_rejection = not (
                            run.load_control and run.load_control.policy is RejectionPolicy.CONTINUE
                            and can_set_aside(error)
                        )
                    else:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.COMMITTED,
                            odoo_id=record_id,
                        )
                self._record_outcomes(workspace_id, run.run_id, (outcome,))
                recorded[row.row_id] = outcome
                publish_progress(
                    changed_rows=1,
                    force=stop_after_unknown or stop_after_rejection,
                )

        if not stop_after_unknown and not stop_after_rejection:
            # Preserve the visible boundary between first-pass writes and
            # relationship completion even when the final create batch is
            # smaller than the configured transport batch.
            publish_progress(force=True)
            completion_stopped, next_transport_batch = self._apply_deferred_relationships(
                workspace_id,
                run.run_id,
                write_rows,
                deferred_by_row,
                recorded,
                metadata,
                by_source,
                source_cache,
                identity_cache,
                executor,
                next_transport_batch=next_transport_batch,
                progress=lambda: publish_progress(changed_rows=1),
                continuation=bool(run.load_control and run.load_control.policy is RejectionPolicy.CONTINUE),
            )
            stop_after_unknown = completion_stopped
            publish_progress(force=True)

        remaining = tuple(
            row
            for row in write_rows
            if recorded[row.row_id].status
            in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }
        )
        if remaining and self.load_controls is not None:
            control = self.load_controls.get_load_control(workspace_id, run.run_id)
            if control is not None:
                control.require_running()
        if remaining:
            self._record_blocked(
                workspace_id,
                run.run_id,
                remaining,
                recorded,
                "The load ended before this record was sent to Odoo",
            )
            publish_progress(changed_rows=len(remaining), force=True)
        statuses = {item.status for item in recorded.values()}
        final_status = (
            ExecutionRunStatus.OUTCOME_UNKNOWN
            if ExecutionRowStatus.OUTCOME_UNKNOWN in statuses
            else (
                ExecutionRunStatus.COMPLETED_WITH_ERRORS
                if statuses.intersection(
                    {
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                        ExecutionRowStatus.FAILED,
                        ExecutionRowStatus.BLOCKED,
                    }
                )
                else ExecutionRunStatus.COMPLETED
            )
        )
        completed_run = self.journal.finish_run(
            workspace_id,
            run.run_id,
            final_status,
            actor=actor,
        )
        if progress_sink is not None:
            progress_sink(completed_run)
        return completed_run

    @staticmethod
    def _classify_recovery(
        snapshot: ExecutionSnapshot,
        run: ExecutionRun,
        recovery: ReconciliationRun,
        *,
        targeted: bool = False,
    ) -> tuple[ExecutionRowAttempt, ...]:
        """Turn one exact read-back into resumable journal states."""

        if (
            recovery.workspace_id != run.workspace_id
            or recovery.execution_run_id != run.run_id
            or recovery.snapshot_hash != run.snapshot_hash
            or recovery.target_hash != run.target_hash
            or recovery.target_database != run.target_database
        ):
            raise WorkspaceError(
                "The recovery report belongs to another load",
                load_failure_code="LOAD_THE_RECOVERY_REPORT_BELONGS_TO_ANOTHER_LOAD",
            )
        allowed_scopes = (
            {"FULL", "RECOVERY_TARGETED"} if targeted else {"FULL"}
        )
        if recovery.readback_scope not in allowed_scopes:
            raise WorkspaceError(
                "The recovery report has an invalid read-back scope",
                load_failure_code="LOAD_THE_RECOVERY_REPORT_HAS_AN_INVALID_READ_BACK_SCOPE",
            )
        expected_identity = (
            run.write_principal_hash,
            run.write_permission_hash,
            run.write_context_hash,
        )
        recovered_identity = (
            recovery.verification_principal_hash,
            recovery.verification_permission_hash,
            recovery.verification_context_hash,
        )
        if any(expected_identity) and (
            recovered_identity != expected_identity
            or not _SHA256.fullmatch(
                recovery.verification_credential_binding_hash
            )
        ):
            raise WorkspaceError(
                "The recovery report used another Odoo principal or context",
                load_failure_code="LOAD_THE_RECOVERY_REPORT_USED_ANOTHER_ODOO_PRINCIPAL_OR_CONTEXT",
            )
        rows = {
            row.row_id: row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        }
        outcomes = {item.row_id: item for item in recovery.rows}
        attempts = {item.row_id: item for item in run.rows}
        projected_requirements = _projected_receipt_requirements(snapshot)
        required_ids = (
            _targeted_recovery_row_ids(snapshot, run)
            if targeted else frozenset(rows)
        )
        if (
            set(rows) != set(attempts)
            or not required_ids.issubset(outcomes)
            or not set(outcomes).issubset(rows)
            or (
                recovery.readback_scope == "FULL"
                and set(outcomes) != set(rows)
            )
        ):
            raise WorkspaceError(
                "The recovery report does not cover every load row",
                load_failure_code="LOAD_THE_RECOVERY_REPORT_DOES_NOT_COVER_EVERY_LOAD_ROW",
            )

        recovery_hash = recovery.semantic_hash
        recovered = []
        for row_id, attempt in attempts.items():
            row = rows[row_id]
            outcome = outcomes.get(row_id)
            if outcome is None:
                # A targeted read omits never-started rows and unrelated
                # committed receipts. Preserve both in the same journal; a
                # full Odoo read is still required by final reconciliation.
                if attempt.status not in {
                    ExecutionRowStatus.COMMITTED,
                    ExecutionRowStatus.PLANNED,
                    ExecutionRowStatus.RETRY_READY,
                }:
                    raise WorkspaceError(
                        "Recovery omitted an unfinished load row",
                        load_failure_code="LOAD_RECOVERY_OMITTED_AN_UNFINISHED_LOAD_ROW",
                    )
                recovered.append(replace(attempt, recovery_hash=recovery_hash))
                continue
            if outcome.execution_status != attempt.status.value:
                raise WorkspaceError(
                    "The recovery report is stale",
                    load_failure_code="LOAD_THE_RECOVERY_REPORT_IS_STALE",
                )
            if outcome.target_model != attempt.target_model:
                raise WorkspaceError(
                    "The recovery report changed a target model",
                    load_failure_code="LOAD_THE_RECOVERY_REPORT_CHANGED_A_TARGET_MODEL",
                )
            if outcome.operation != attempt.operation:
                raise WorkspaceError(
                    "The recovery report changed a write operation",
                    load_failure_code="LOAD_THE_RECOVERY_REPORT_CHANGED_A_WRITE_OPERATION",
                )

            status = attempt.status
            odoo_id = attempt.odoo_id
            safe_error = attempt.safe_error
            deferred_fields = {
                intent.field
                for intent in row.fields
                if intent.defer_on_create and intent.action == "SET_VALUE"
            }
            differing_fields = set(outcome.differing_fields)
            deferred_difference = bool(differing_fields) and differing_fields.issubset(
                deferred_fields
            )
            projected_keys = {
                (item.projection_field, item.target_model)
                for item in attempt.projected_receipts
            }
            projection_pending = bool(
                projected_requirements.get(row_id, frozenset()).difference(
                    projected_keys
                )
            )

            if status is ExecutionRowStatus.COMMITTED:
                if (
                    outcome.status is not ReconciliationRowStatus.VERIFIED
                    or outcome.odoo_id != odoo_id
                ):
                    raise WorkspaceError(
                        "A completed earlier component no longer matches Odoo",
                        load_failure_code="LOAD_A_COMPLETED_EARLIER_COMPONENT_NO_LONGER_MATCHES_ODOO",
                    )
            elif status in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }:
                if outcome.status is not ReconciliationRowStatus.NOT_WRITTEN:
                    raise WorkspaceError(
                        "An unstarted row has conflicting recovery evidence",
                        load_failure_code="LOAD_AN_UNSTARTED_ROW_HAS_CONFLICTING_RECOVERY_EVIDENCE",
                    )
            elif status is ExecutionRowStatus.IN_FLIGHT:
                if outcome.status is ReconciliationRowStatus.VERIFIED:
                    if outcome.odoo_id is None:
                        raise WorkspaceError(
                            "Recovery found no durable Odoo receipt",
                            load_failure_code="LOAD_RECOVERY_FOUND_NO_DURABLE_ODOO_RECEIPT",
                        )
                    status = (
                        ExecutionRowStatus.PARTIALLY_APPLIED
                        if projection_pending or deferred_fields
                        else ExecutionRowStatus.COMMITTED
                    )
                    odoo_id = outcome.odoo_id
                    safe_error = (
                        "Created; generated relationship read-back pending"
                        if projection_pending
                        else (
                            "Created; deferred relationship update pending"
                            if deferred_fields
                            else ""
                        )
                    )
                elif (
                    attempt.transport_phase == "CREATE"
                    and outcome.status is ReconciliationRowStatus.NOT_APPLIED
                    and outcome.retry_safe
                ):
                    status = ExecutionRowStatus.RETRY_READY
                    odoo_id = None
                    safe_error = "Read-back proved that the interrupted create was not applied"
                elif (
                    attempt.transport_phase in {"CREATE", "COMPLETION"}
                    and outcome.status is ReconciliationRowStatus.DIFFERENT
                    and outcome.odoo_id is not None
                    and deferred_difference
                ):
                    status = ExecutionRowStatus.PARTIALLY_APPLIED
                    odoo_id = outcome.odoo_id
                    safe_error = "Created record is waiting for its reviewed relationships"
                elif (
                    attempt.transport_phase == "UPDATE"
                    and outcome.status is ReconciliationRowStatus.DIFFERENT
                    and outcome.odoo_id == odoo_id
                    and differing_fields
                    and differing_fields.issubset(set(attempt.field_names))
                ):
                    status = ExecutionRowStatus.RETRY_READY
                    safe_error = "Read-back proved that reviewed update fields still differ"
                else:
                    raise WorkspaceError(
                        "The interrupted Odoo outcome is not safe to resume",
                        load_failure_code="LOAD_THE_INTERRUPTED_ODOO_OUTCOME_IS_NOT_SAFE_TO_RESUME",
                    )
            elif status is ExecutionRowStatus.PARTIALLY_APPLIED:
                if (
                    outcome.status is ReconciliationRowStatus.VERIFIED
                    and outcome.odoo_id == odoo_id
                ):
                    status = (
                        ExecutionRowStatus.PARTIALLY_APPLIED
                        if projection_pending
                        else ExecutionRowStatus.COMMITTED
                    )
                    safe_error = (
                        "Created; generated relationship read-back pending"
                        if projection_pending
                        else ""
                    )
                elif (
                    outcome.status is ReconciliationRowStatus.DIFFERENT
                    and outcome.odoo_id == odoo_id
                    and deferred_difference
                ):
                    safe_error = "Created record is waiting for its reviewed relationships"
                else:
                    raise WorkspaceError(
                        "The partial relationship result is not safe to resume",
                        load_failure_code="LOAD_THE_PARTIAL_RELATIONSHIP_RESULT_IS_NOT_SAFE_TO_RESUME",
                    )
            elif (
                run.load_control is not None
                and run.load_control.policy is RejectionPolicy.CONTINUE
                and run.load_control.state is LoadControlState.PAUSED
                and status in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
                and outcome.status in {ReconciliationRowStatus.NOT_WRITTEN, ReconciliationRowStatus.VERIFIED, ReconciliationRowStatus.DIFFERENT}
                and outcome.odoo_id == odoo_id
            ):
                # Retain a known rejection or dependency blocker. Recovery never
                # makes either eligible for another write.
                pass
            else:
                raise WorkspaceError(
                    "This stopped load cannot resume; run Check changes again",
                    load_failure_code="LOAD_THIS_STOPPED_LOAD_CANNOT_RESUME_RUN_CHECK_CHANGES_AGAIN",
                )
            recovered.append(
                replace(
                    attempt,
                    status=status,
                    odoo_id=odoo_id,
                    safe_error=safe_error,
                    recovery_hash=recovery_hash,
                )
            )
        return tuple(recovered)

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

    @staticmethod
    def _assert_create_rows_still_absent(
        write_rows: Sequence[ExecutionRow],
        metadata: Mapping[str, ExecutionDataset],
        executor: OdooWriteExecutor,
    ) -> None:
        """Fail before journaling when a reviewed create key now exists."""

        nested = NestedIdentityLookup(write_rows, metadata, executor.find_ids_many,
                                      page_size=MAX_IDENTITY_LOOKUP_KEYS)
        by_model: dict[str, list[tuple[tuple[str, str, Any], ...]]] = {}
        for row in write_rows:
            if row.disposition != "CREATE":
                continue
            dataset = metadata[row.dataset]
            domain = _identity_domain(
                dataset.identity_fields,
                row.business_identity,
                dataset.scope_fields,
                row.business_scope,
            )
            by_model.setdefault(row.target_model, []).append(domain)
        try:
            for model in sorted(by_model):
                domains = by_model[model]
                for start in range(0, len(domains), MAX_IDENTITY_LOOKUP_KEYS):
                    page = tuple(domains[start : start + MAX_IDENTITY_LOOKUP_KEYS])
                    matches = executor.find_ids_many(model, nested.translate(model, page))
                    if len(matches) != len(page):
                        raise WorkspaceError(
                            "Odoo returned an incomplete create-key check",
                            load_failure_code="LOAD_ODOO_RETURNED_AN_INCOMPLETE_CREATE_KEY_CHECK",
                        )
                    if any(identifiers for identifiers in matches):
                        raise WorkspaceError(
                            "An Odoo business key marked for creation now exists; "
                            "run destination preflight again",
                            load_failure_code="LOAD_AN_ODOO_BUSINESS_KEY_MARKED_FOR_CREATION_NOW_EXISTS_RUN_DESTINATION_PREFLIGHT_AGAIN",
                        )
        except OdooWriteRejected as error:
            raise WorkspaceError(
                "Odoo create-key verification failed before loading",
                load_failure_code="LOAD_ODOO_CREATE_KEY_VERIFICATION_FAILED_BEFORE_LOADING",
            ) from error

    @staticmethod
    def _resolve_identity_crosswalk(
        write_rows: Sequence[ExecutionRow],
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        executor: OdooWriteExecutor,
    ) -> dict[str, int]:
        """Bulk-verify every existing target before journaling any write."""

        nested = NestedIdentityLookup(by_source.values(), metadata, executor.find_ids_many,
                                      page_size=MAX_IDENTITY_LOOKUP_KEYS)
        requirements: dict[str, dict[str, Any]] = {}

        def add_requirement(
            model: str,
            domain: tuple[tuple[str, str, Any], ...],
            expected_binding_hash: str,
            *aliases: str,
        ) -> None:
            key = _domain_cache_key(model, domain)
            current = requirements.get(key)
            if current is None:
                requirements[key] = {
                    "model": model,
                    "domain": domain,
                    "expected": expected_binding_hash,
                    "aliases": {key, *aliases},
                }
                return
            if (
                current["model"] != model
                or current["domain"] != domain
                or (
                    current["expected"]
                    and expected_binding_hash
                    and current["expected"] != expected_binding_hash
                )
            ):
                raise WorkspaceError(
                    "The reviewed Odoo identity bindings conflict",
                    load_failure_code="LOAD_THE_REVIEWED_ODOO_IDENTITY_BINDINGS_CONFLICT",
                )
            if not current["expected"]:
                current["expected"] = expected_binding_hash
            current["aliases"].update(aliases)

        def add_row(row: ExecutionRow) -> None:
            dataset = metadata[row.dataset]
            domain = _identity_domain(
                dataset.identity_fields,
                row.business_identity,
                dataset.scope_fields,
                row.business_scope,
            )
            add_requirement(
                row.target_model,
                domain,
                row.target_binding_hash,
                _identity_cache_key(row),
            )

        for row in write_rows:
            if row.disposition == "UPDATE":
                add_row(row)
            for intent in row.fields:
                if intent.action != "SET_VALUE" or intent.kind != "relation":
                    continue
                values = (
                    intent.value
                    if isinstance(intent.value, tuple)
                    else (intent.value,)
                )
                bindings = intent.target_binding_hashes or ("",) * len(values)
                for value, binding_hash in zip(values, bindings, strict=True):
                    if (
                        isinstance(value, LogicalReference)
                        and value.origin == "incoming"
                    ):
                        if value.dataset is None:
                            raise WorkspaceError(
                                "An incoming relationship is incomplete",
                                load_failure_code="LOAD_INCOMING_RELATIONSHIP_INCOMPLETE",
                            )
                        referenced = by_source.get(
                            (value.dataset, _portable_key(value.key))
                        )
                        if referenced is None:
                            raise WorkspaceError(
                                "A related prepared row could not be found",
                                load_failure_code="LOAD_RELATED_PREPARED_ROW_MISSING",
                            )
                        if referenced.disposition != "CREATE":
                            add_row(referenced)
                        continue
                    if isinstance(value, BusinessReference | LogicalReference):
                        key, scope = value.key, value.scope
                    else:
                        raise WorkspaceError(
                            "A relationship is not expressed by a business key",
                            load_failure_code="LOAD_A_RELATIONSHIP_IS_NOT_EXPRESSED_BY_A_BUSINESS_KEY",
                        )
                    domain = _identity_domain(
                        intent.related_identity_fields,
                        key,
                        intent.related_scope_fields,
                        scope,
                    )
                    add_requirement(
                        intent.related_model,
                        domain,
                        binding_hash,
                    )

        identity_cache: dict[str, int] = {}
        by_model: dict[str, list[dict[str, Any]]] = {}
        for requirement in requirements.values():
            by_model.setdefault(requirement["model"], []).append(requirement)
        try:
            for model in sorted(by_model):
                model_requirements = sorted(
                    by_model[model],
                    key=lambda item: _domain_cache_key(model, item["domain"]),
                )
                for start in range(
                    0,
                    len(model_requirements),
                    MAX_IDENTITY_LOOKUP_KEYS,
                ):
                    page = model_requirements[
                        start : start + MAX_IDENTITY_LOOKUP_KEYS
                    ]
                    matches = executor.find_ids_many(
                        model,
                        nested.translate(model, tuple(item["domain"] for item in page)),
                    )
                    if len(matches) != len(page):
                        raise WorkspaceError(
                            "Odoo returned an incomplete identity crosswalk",
                            load_failure_code="LOAD_ODOO_RETURNED_AN_INCOMPLETE_IDENTITY_CROSSWALK",
                        )
                    for requirement, identifiers in zip(
                        page,
                        matches,
                        strict=True,
                    ):
                        if len(identifiers) != 1:
                            raise WorkspaceError(
                                f"The Odoo business key for {model} matched "
                                f"{len(identifiers)} records; compare again",
                                load_failure_code="LOAD_THE_ODOO_BUSINESS_KEY_FOR_CONFIGURED_CONTEXT_MATCHED_CONFIGURED_CONTEXT_RECORDS_COMPARE_AGAIN",
                            )
                        identifier = identifiers[0]
                        expected = requirement["expected"]
                        if expected and target_record_binding_hash(
                            model, identifier
                        ) != expected:
                            raise WorkspaceError(
                                "An Odoo business key now targets a different record; compare again",
                                load_failure_code="LOAD_AN_ODOO_BUSINESS_KEY_NOW_TARGETS_A_DIFFERENT_RECORD_COMPARE_AGAIN",
                            )
                        for alias in requirement["aliases"]:
                            identity_cache[alias] = identifier
        except OdooIdentityLookupUnavailable as error:
            raise WorkspaceError(
                f"{error} This load stopped before sending any records. "
                "Check the Odoo connection, then run Check changes again.",
                load_failure_code=error.load_failure_code,
            ) from error
        except OdooWriteRejected as error:
            raise WorkspaceError(
                "Odoo identity verification failed before loading",
                load_failure_code="LOAD_ODOO_IDENTITY_VERIFICATION_FAILED_BEFORE_LOADING",
            ) from error
        return identity_cache

    @staticmethod
    def _require_dependency_receipts(
        row: ExecutionRow,
        row_by_id: Mapping[str, ExecutionRow],
        recorded: Mapping[str, ExecutionRowAttempt],
    ) -> None:
        """Require journalled create receipts before a dependent write."""

        for intent in row.fields:
            if intent.defer_on_create:
                continue
            for dependency_row_id in intent.dependency_row_ids:
                dependency = row_by_id[dependency_row_id]
                if dependency.disposition != "CREATE":
                    continue
                attempt = recorded.get(dependency_row_id)
                if (
                    attempt is None
                    or attempt.status
                    not in {
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.PARTIALLY_APPLIED,
                    }
                    or attempt.odoo_id is None
                ):
                    raise WorkspaceError(
                        "A required Odoo create receipt was not journalled",
                        load_failure_code="LOAD_A_REQUIRED_ODOO_CREATE_RECEIPT_WAS_NOT_JOURNALLED",
                    )
                if intent.incoming_projection_field and not any(
                    receipt.projection_field
                    == intent.incoming_projection_field
                    and receipt.target_model == intent.related_model
                    for receipt in attempt.projected_receipts
                ):
                    raise WorkspaceError(
                        "A generated Odoo relationship receipt was not journalled",
                        load_failure_code="LOAD_A_GENERATED_ODOO_RELATIONSHIP_RECEIPT_WAS_NOT_JOURNALLED",
                    )

    def _record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Measure each durable outcome write without changing its boundary."""

        with observe_load_local_phase("journal_after_call"):
            self.journal.record_outcomes(workspace_id, run_id, rows)

    def _start_transport_batch(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        *,
        phase: str,
        component: int,
        page: int,
        batch: int,
        known_ids: Mapping[str, int | None] | None = None,
    ) -> None:
        """Persist one exact in-flight transport batch before sending it."""

        if phase not in {"CREATE", "UPDATE", "COMPLETION"}:
            raise WorkspaceError(
                "Execution transport phase is invalid",
                load_failure_code="LOAD_EXECUTION_TRANSPORT_PHASE_IS_INVALID",
            )
        attempts = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.IN_FLIGHT,
                attempt=recorded[row.row_id].attempt + 1,
                odoo_id=(known_ids or {}).get(row.row_id),
                safe_error="",
                diagnostic=None,
                schedule_component=component,
                transport_page=page,
                transport_batch=batch,
                transport_phase=phase,
            )
            for row in rows
        )
        with observe_load_local_phase("journal_before_call"):
            self.journal.record_batch_started(
                workspace_id,
                run_id,
                attempts,
            )
        observe_journal_batch(attempts)
        recorded.update({item.row_id: item for item in attempts})

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

    @staticmethod
    def _deferred_create_intents(
        row: ExecutionRow,
    ) -> tuple[FieldIntent, ...]:
        """Return the exact completion fields frozen by row scheduling."""

        return tuple(
            intent
            for intent in row.fields
            if intent.defer_on_create and intent.action == "SET_VALUE"
        )

    def _ensure_projected_receipts(
        self,
        workspace_id: str,
        run_id: str,
        consumer_rows: Sequence[ExecutionRow],
        row_by_id: Mapping[str, ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        metadata: Mapping[str, ExecutionDataset],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
        requirements: Mapping[str, frozenset[tuple[str, str]]],
        deferred_by_row: Mapping[str, tuple[FieldIntent, ...]],
    ) -> None:
        """Read and journal exact generated records before their consumers."""

        grouped: dict[
            tuple[str, str, str],
            dict[str, tuple[ExecutionRow, int]],
        ] = {}
        for consumer in consumer_rows:
            for intent in consumer.fields:
                projection_field = intent.incoming_projection_field
                if not projection_field:
                    continue
                for dependency_row_id in intent.dependency_row_ids:
                    dependency = row_by_id[dependency_row_id]
                    cache_key = (
                        dependency.dataset,
                        _portable_key(dependency.source_identity),
                        intent.related_model,
                        projection_field,
                    )
                    if cache_key in source_cache:
                        continue
                    if dependency.disposition == "CREATE":
                        attempt = recorded.get(dependency_row_id)
                        if (
                            attempt is None
                            or attempt.status
                            not in {
                                ExecutionRowStatus.PARTIALLY_APPLIED,
                                ExecutionRowStatus.COMMITTED,
                            }
                            or attempt.odoo_id is None
                        ):
                            raise WorkspaceError(
                                "A generated relationship source was not created",
                                load_failure_code="LOAD_A_GENERATED_RELATIONSHIP_SOURCE_WAS_NOT_CREATED",
                            )
                        source_id = attempt.odoo_id
                    else:
                        source_id = self._find_row_id(
                            dependency,
                            metadata[dependency.dataset],
                            identity_cache,
                            executor,
                        )
                    grouped.setdefault(
                        (
                            dependency.target_model,
                            projection_field,
                            intent.related_model,
                        ),
                        {},
                    )[dependency_row_id] = (dependency, source_id)

        observed: list[
            tuple[ExecutionRow, str, str, int]
        ] = []
        for (
            source_model,
            projection_field,
            target_model,
        ), dependencies in sorted(grouped.items()):
            ordered = tuple(
                dependencies[row_id] for row_id in sorted(dependencies)
            )
            for start in range(0, len(ordered), MAX_PROJECTED_RECEIPT_IDS):
                page = ordered[start : start + MAX_PROJECTED_RECEIPT_IDS]
                projected_ids = executor.read_projected_ids(
                    source_model,
                    tuple(source_id for _row, source_id in page),
                    projection_field,
                    target_model,
                )
                if len(projected_ids) != len(page):
                    raise WorkspaceError(
                        "Odoo generated-record read-back was incomplete",
                        load_failure_code="LOAD_ODOO_GENERATED_RECORD_READ_BACK_WAS_INCOMPLETE",
                    )
                observed.extend(
                    (
                        row,
                        projection_field,
                        target_model,
                        projected_id,
                    )
                    for (row, _source_id), projected_id in zip(
                        page,
                        projected_ids,
                        strict=True,
                    )
                )

        new_by_row: dict[str, list[ProjectedOdooReceipt]] = {}
        for row, projection_field, target_model, projected_id in observed:
            if row.disposition != "CREATE":
                continue
            new_by_row.setdefault(row.row_id, []).append(
                ProjectedOdooReceipt(
                    projection_field=projection_field,
                    target_model=target_model,
                    odoo_id=projected_id,
                )
            )
        outcomes: list[ExecutionRowAttempt] = []
        for row_id, new_receipts in sorted(new_by_row.items()):
            attempt = recorded[row_id]
            if attempt.status is not ExecutionRowStatus.PARTIALLY_APPLIED:
                raise WorkspaceError(
                    "A generated relationship source journal is inconsistent",
                    load_failure_code="LOAD_A_GENERATED_RELATIONSHIP_SOURCE_JOURNAL_IS_INCONSISTENT",
                )
            receipts = {
                (item.projection_field, item.target_model): item
                for item in attempt.projected_receipts
            }
            for receipt in new_receipts:
                key = (receipt.projection_field, receipt.target_model)
                previous = receipts.get(key)
                if previous is not None and previous.odoo_id != receipt.odoo_id:
                    raise WorkspaceError(
                        "An Odoo generated-record receipt changed",
                        load_failure_code="LOAD_AN_ODOO_GENERATED_RECORD_RECEIPT_CHANGED",
                    )
                receipts[key] = receipt
            missing = requirements.get(row_id, frozenset()).difference(receipts)
            waiting_for_relationships = row_id in deferred_by_row
            outcomes.append(
                replace(
                    attempt,
                    status=(
                        ExecutionRowStatus.PARTIALLY_APPLIED
                        if missing or waiting_for_relationships
                        else ExecutionRowStatus.COMMITTED
                    ),
                    safe_error=(
                        "Created; generated relationship read-back pending"
                        if missing
                        else (
                            "Created; deferred relationship update pending"
                            if waiting_for_relationships
                            else ""
                        )
                    ),
                    projected_receipts=tuple(
                        receipts[key] for key in sorted(receipts)
                    ),
                )
            )
        if outcomes:
            self._record_outcomes(workspace_id, run_id, outcomes)
            recorded.update({item.row_id: item for item in outcomes})

        for row, projection_field, target_model, projected_id in observed:
            source_cache[
                (
                    row.dataset,
                    _portable_key(row.source_identity),
                    target_model,
                    projection_field,
                )
            ] = projected_id

    def _apply_deferred_relationships(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        deferred_by_row: Mapping[str, tuple[FieldIntent, ...]],
        recorded: dict[str, ExecutionRowAttempt],
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
        next_transport_batch: int,
        progress: Callable[[], None],
        continuation: bool = False,
    ) -> tuple[bool, int]:
        """Patch deferred create relationships after all first-pass creates."""

        dependencies = DependentCompletions(rows, tuple(
            row.row_id for row in rows
            if recorded[row.row_id].status in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
            or (recorded[row.row_id].status is ExecutionRowStatus.PARTIALLY_APPLIED and recorded[row.row_id].diagnostic is not None)
        )) if continuation else None
        for row in rows:
            intents = deferred_by_row.get(row.row_id)
            if not intents:
                continue
            attempt = recorded[row.row_id]
            if (
                attempt.status is not ExecutionRowStatus.PARTIALLY_APPLIED
                or attempt.odoo_id is None
                or (continuation and attempt.diagnostic is not None)
            ):
                continue
            if dependencies is not None and dependencies.blocks(row.row_id):
                outcome = replace(attempt, safe_error="Created; relationship completion is waiting because a required record was rejected")
                self._record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                progress()
                continue
            record_id = attempt.odoo_id
            try:
                values = {
                    intent.field: self._relation_value(
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    for intent in intents
                }
            except (WorkspaceError, OdooWriteRejected) as error:
                outcome = replace(
                    attempt,
                    safe_error=(
                        "Record was created, but its deferred relationship "
                        f"update failed: {error}"
                    ),
                )
                self._record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                if dependencies is not None:
                    dependencies.reject(row.row_id)
                progress()
                continue
            self._start_transport_batch(
                workspace_id,
                run_id,
                (row,),
                recorded,
                phase="COMPLETION",
                component=row.schedule_component,
                page=0,
                batch=next_transport_batch,
                known_ids={row.row_id: record_id},
            )
            next_transport_batch += 1
            attempt = recorded[row.row_id]
            if attempt.odoo_id != record_id:
                raise WorkspaceError(
                    "The deferred relationship target changed in the execution journal",
                    load_failure_code="LOAD_THE_DEFERRED_RELATIONSHIP_TARGET_CHANGED_IN_THE_EXECUTION_JOURNAL",
                )
            progress()
            try:
                with observe_load_transport(row.target_model, "write", tuple(values), 1):
                    executor.update_row(row.target_model, record_id, values)
            except OdooWriteOutcomeUnknown as error:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                    safe_error=str(error),
                    diagnostic=_write_diagnostic(error, row, method="write"),
                )
                self._record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                progress()
                return True, next_transport_batch
            except OdooWriteRejected as error:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.PARTIALLY_APPLIED,
                    diagnostic=_write_diagnostic(error, row, method="write"),
                    safe_error=(
                        "Record was created, but its deferred relationship "
                        f"update failed: {error}"
                    ),
                )
                if not continuation or not can_set_aside(error):
                    self._record_outcomes(workspace_id, run_id, (outcome,))
                    recorded[row.row_id] = outcome
                    progress()
                    return True, next_transport_batch
                if dependencies is not None:
                    dependencies.reject(row.row_id)
            else:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.COMMITTED,
                    safe_error="",
                )
            self._record_outcomes(workspace_id, run_id, (outcome,))
            recorded[row.row_id] = outcome
            progress()
        return False, next_transport_batch

    def _row_values(
        self,
        row: ExecutionRow,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
        *,
        protected_values: Mapping[str, Any] | None = None,
        import_relations: bool = False,
        skip_fields: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        field_name = ""
        try:
            for intent in row.fields:
                field_name = intent.field
                if intent.action in {"OMIT", "EXPECT_PROTECTED"} or intent.field in skip_fields:
                    continue
                if intent.action == "SET_NULL":
                    values[intent.field] = "" if import_relations else None
                elif intent.action == "SET_PROTECTED" and intent.kind == "relation":
                    if (
                        protected_values is None
                        or intent.protected_value_hash not in protected_values
                        or type(protected_values[intent.protected_value_hash]) is not int
                        or protected_values[intent.protected_value_hash] <= 0
                    ):
                        raise WorkspaceError(
                            f"Protected relationship is unavailable for "
                            f"{row.target_model}.{intent.field}",
                            load_failure_code="LOAD_PROTECTED_RELATIONSHIP_IS_UNAVAILABLE_FOR_MODEL_FIELD",
                        )
                    identifier = protected_values[intent.protected_value_hash]
                    if import_relations:
                        values[f"{intent.field}/.id"] = str(identifier)
                    else:
                        values[intent.field] = identifier
                elif intent.kind == "scalar":
                    raw_value = intent.value
                    if intent.action == "SET_PROTECTED":
                        if (
                            protected_values is None
                            or intent.protected_value_hash not in protected_values
                        ):
                            raise WorkspaceError(
                                f"Protected value is unavailable for {row.target_model}.{intent.field}",
                                load_failure_code="LOAD_PROTECTED_VALUE_IS_UNAVAILABLE_FOR_MODEL_FIELD",
                            )
                        raw_value = protected_values[intent.protected_value_hash]
                    values[intent.field] = (
                        _odoo_import_scalar(raw_value)
                        if import_relations
                        else _odoo_scalar(raw_value)
                    )
                elif import_relations:
                    field, value = self._import_relation_value(
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    values[field] = value
                else:
                    values[intent.field] = self._relation_value(
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
        except Exception as error:
            record_load_failure(error, stage="prepare_write_values", model=row.target_model, fields=(field_name,))
            raise
        if not values:
            raise WorkspaceError(
                "A planned write has no permitted field values",
                load_failure_code="LOAD_A_PLANNED_WRITE_HAS_NO_PERMITTED_FIELD_VALUES",
            )
        return values

    def _import_relation_value(
        self,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> tuple[str, object]:
        """Choose exact Odoo import identities for one remote relation field."""

        value = intent.value
        if isinstance(value, tuple):
            if intent.relation_operation not in {"replace", "add"}:
                raise WorkspaceError(
                    f"Relationship field {intent.field} is outside the remote "
                    "many2many import slice",
                    load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2MANY_IMPORT_SLICE",
                )
            for item in value:
                if isinstance(item, BusinessReference):
                    if item.model != intent.related_model:
                        raise WorkspaceError(
                            f"Relationship field {intent.field} targets another "
                            "Odoo model",
                            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_TARGETS_ANOTHER_ODOO_MODEL",
                        )
                elif not (
                    isinstance(item, LogicalReference)
                    and item.origin == "incoming"
                    and item.dataset is not None
                ):
                    raise WorkspaceError(
                        f"Relationship field {intent.field} is outside the remote "
                        "many2many import slice",
                        load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2MANY_IMPORT_SLICE",
                    )
            identifiers = tuple(
                dict.fromkeys(
                    self._relation_reference_id(
                        item,
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    for item in value
                )
            )
            return (
                f"{intent.field}/.id",
                ",".join(str(identifier) for identifier in identifiers),
            )
        if intent.relation_operation != "replace":
            raise WorkspaceError(
                f"Relationship field {intent.field} is outside the remote "
                "many2one import slice",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
            )
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if not intent.incoming_projection_field:
                if value.dataset is None:
                    raise WorkspaceError(
                        "An incoming relationship has no source table identity",
                        load_failure_code="LOAD_AN_INCOMING_RELATIONSHIP_HAS_NO_SOURCE_TABLE_IDENTITY",
                    )
                referenced = by_source.get(
                    (value.dataset, _portable_key(value.key))
                )
                if (
                    referenced is not None
                    and referenced.disposition in {"UPDATE", "UNCHANGED"}
                ):
                    identifier = self._relation_reference_id(
                        value,
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    return f"{intent.field}/.id", str(identifier)
                return (
                    f"{intent.field}/id",
                    self._relation_external_id(intent, by_source),
                )
            identifier = self._relation_reference_id(
                value,
                intent,
                metadata,
                by_source,
                source_cache,
                identity_cache,
                executor,
            )
            return f"{intent.field}/.id", str(identifier)
        if isinstance(value, BusinessReference):
            if value.model != intent.related_model:
                raise WorkspaceError(
                    f"Relationship field {intent.field} targets another Odoo model",
                    load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_TARGETS_ANOTHER_ODOO_MODEL",
                )
            identifier = self._relation_reference_id(
                value,
                intent,
                metadata,
                by_source,
                source_cache,
                identity_cache,
                executor,
            )
            return f"{intent.field}/.id", str(identifier)
        raise WorkspaceError(
            f"Relationship field {intent.field} is outside the remote "
            "many2one import slice",
            load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
        )

    @staticmethod
    def _relation_external_id(
        intent: FieldIntent,
        by_source: Mapping[tuple[str, str], ExecutionRow],
    ) -> str:
        """Resolve an earlier remote import's many2one External ID."""

        value = intent.value
        if (
            intent.relation_operation != "replace"
            or not isinstance(value, LogicalReference)
            or value.origin != "incoming"
            or value.dataset is None
        ):
            raise WorkspaceError(
                f"Relationship field {intent.field} is outside the remote "
                "many2one import slice",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_IS_OUTSIDE_THE_REMOTE_MANY2ONE_IMPORT_SLICE",
            )
        referenced = by_source.get(
            (value.dataset, _portable_key(value.key))
        )
        if (
            referenced is None
            or referenced.disposition != "CREATE"
            or not referenced.proposed_external_id
            or referenced.target_model != intent.related_model
        ):
            raise WorkspaceError(
                f"Relationship field {intent.field} has no earlier imported "
                "External ID",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_HAS_NO_EARLIER_IMPORTED_EXTERNAL_ID",
            )
        return referenced.proposed_external_id

    def _relation_value(
        self,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> object:
        value = intent.value
        if isinstance(value, tuple):
            identifiers = tuple(
                dict.fromkeys(
                    self._relation_reference_id(
                        item,
                        intent,
                        metadata,
                        by_source,
                        source_cache,
                        identity_cache,
                        executor,
                    )
                    for item in value
                )
            )
            # Update snapshots already contain the final canonical set and use
            # replace.  For creates, add starts from an empty relation and
            # remove therefore also has a deterministic final empty set.
            if intent.relation_operation in {"replace", "add"}:
                return [[6, 0, list(identifiers)]]
            if intent.relation_operation == "remove":
                return [[6, 0, []]]
            raise WorkspaceError(
                f"Relationship field {intent.field} has an unsupported operation",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_HAS_AN_UNSUPPORTED_OPERATION",
            )
        if intent.relation_operation != "replace":
            raise WorkspaceError(
                f"Relationship field {intent.field} must use replace",
                load_failure_code="LOAD_RELATIONSHIP_FIELD_FIELD_MUST_USE_REPLACE",
            )
        return self._relation_reference_id(
            value,
            intent,
            metadata,
            by_source,
            source_cache,
            identity_cache,
            executor,
        )

    def _relation_reference_id(
        self,
        value: object,
        intent: FieldIntent,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str, str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if value.dataset is None:
                raise WorkspaceError(
                    "An incoming relationship is incomplete",
                    load_failure_code="LOAD_INCOMING_RELATIONSHIP_INCOMPLETE",
                )
            source_key = (
                value.dataset,
                _portable_key(value.key),
                intent.related_model,
                intent.incoming_projection_field,
            )
            if source_key in source_cache:
                return source_cache[source_key]
            referenced = by_source.get(
                (value.dataset, _portable_key(value.key))
            )
            if referenced is None:
                raise WorkspaceError(
                    "A related prepared row could not be found",
                    load_failure_code="LOAD_RELATED_PREPARED_ROW_MISSING",
                )
            if intent.incoming_projection_field:
                raise WorkspaceError(
                    "A generated Odoo relationship receipt was not journalled",
                    load_failure_code="LOAD_A_GENERATED_ODOO_RELATIONSHIP_RECEIPT_WAS_NOT_JOURNALLED",
                )
            if referenced.disposition == "CREATE":
                raise WorkspaceError(
                    "A related Odoo create did not complete",
                    load_failure_code="LOAD_A_RELATED_ODOO_CREATE_DID_NOT_COMPLETE",
                )
            related_id = self._find_row_id(
                referenced,
                metadata[referenced.dataset],
                identity_cache,
                executor,
            )
            source_cache[
                (
                    value.dataset,
                    _portable_key(value.key),
                    referenced.target_model,
                    "",
                )
            ] = related_id
            return related_id
        if isinstance(value, BusinessReference | LogicalReference):
            key, scope = value.key, value.scope
        else:
            raise WorkspaceError(
                "A relationship is not expressed by a business key",
                load_failure_code="LOAD_A_RELATIONSHIP_IS_NOT_EXPRESSED_BY_A_BUSINESS_KEY",
            )
        domain = _identity_domain(
            intent.related_identity_fields,
            key,
            intent.related_scope_fields,
            scope,
        )
        cache_key = _domain_cache_key(intent.related_model, domain)
        return self._find_unique(
            intent.related_model,
            domain,
            cache_key,
            identity_cache,
            executor,
        )

    def _find_row_id(
        self,
        row: ExecutionRow,
        dataset: ExecutionDataset,
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        cache_key = _identity_cache_key(row)
        if cache_key in identity_cache:
            return identity_cache[cache_key]
        domain = _identity_domain(
            dataset.identity_fields,
            row.business_identity,
            dataset.scope_fields,
            row.business_scope,
        )
        return self._find_unique(
            row.target_model,
            domain,
            cache_key,
            identity_cache,
            executor,
        )

    @staticmethod
    def _find_unique(
        model: str,
        domain: tuple[tuple[str, str, Any], ...],
        cache_key: str,
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        if cache_key in identity_cache:
            return identity_cache[cache_key]
        raise WorkspaceError(
            "The reviewed Odoo identity crosswalk is incomplete",
            load_failure_code="LOAD_THE_REVIEWED_ODOO_IDENTITY_CROSSWALK_IS_INCOMPLETE",
        )

    def _record_blocked(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        reason: str,
    ) -> None:
        outcomes = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.BLOCKED,
                safe_error=reason,
            )
            for row in rows
            if recorded[row.row_id].status
            in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }
        )
        self._record_outcomes(workspace_id, run_id, outcomes)
        recorded.update({item.row_id: item for item in outcomes})


def _identity_domain(
    identity_fields: tuple[str, ...],
    identity: tuple[Any, ...],
    scope_fields: tuple[str, ...],
    scope: tuple[Any, ...],
) -> tuple[tuple[str, str, Any], ...]:
    if (
        not identity_fields
        or len(identity_fields) != len(identity)
        or len(scope_fields) != len(scope)
    ):
        raise WorkspaceError(
            "The Odoo business-key shape is incomplete",
            load_failure_code="LOAD_THE_ODOO_BUSINESS_KEY_SHAPE_IS_INCOMPLETE",
        )
    terms: list[tuple[str, str, Any]] = []
    for field_name, value in zip(
            (*identity_fields, *scope_fields),
            (*identity, *scope),
            strict=True,
    ):
        if isinstance(value, BusinessReference):
            nested = _identity_domain(value.target_fields, value.key,
                                      value.target_scope_fields, value.scope)
            terms.extend((f"{field_name}.{name}", operator, scalar)
                         for name, operator, scalar in nested)
        else:
            terms.append((field_name, "=", _odoo_scalar(value)))
    return tuple(terms)


def _planned_deferred_create_count(
    snapshot: ExecutionSnapshot,
    *,
    create_batch_rows: int,
) -> int:
    """Count create rows expected to need the reviewed second relation pass."""

    validated_create_batch_rows(create_batch_rows)
    return len(
        {
            item.row_id
            for item in snapshot.relationship_plan.completions
        }
    )


def _projected_receipt_requirements(
    snapshot: ExecutionSnapshot,
) -> dict[str, frozenset[tuple[str, str]]]:
    """Index exact generated-record receipts required by incoming relations."""

    requirements: dict[str, set[tuple[str, str]]] = {}
    for row in snapshot.rows:
        for intent in row.fields:
            if not intent.incoming_projection_field:
                continue
            for dependency_row_id in intent.dependency_row_ids:
                requirements.setdefault(dependency_row_id, set()).add(
                    (intent.incoming_projection_field, intent.related_model)
                )
    return {
        row_id: frozenset(items)
        for row_id, items in requirements.items()
    }


def validated_create_batch_rows(value: int | str) -> int:
    """Return one bounded Odoo request size chosen for this load run."""

    if isinstance(value, bool):
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        )
    try:
        batch_rows = int(value)
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        ) from error
    if str(value).strip() != str(batch_rows):
        raise WorkspaceError(
            "Rows per Odoo batch must be a whole number",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_A_WHOLE_NUMBER",
        )
    if batch_rows < 1 or batch_rows > MAX_CREATE_BATCH_ROWS:
        raise WorkspaceError(
            f"Rows per Odoo batch must be between 1 and {MAX_CREATE_BATCH_ROWS}",
            load_failure_code="LOAD_ROWS_PER_ODOO_BATCH_MUST_BE_BETWEEN_1_AND_CONFIGURED_CONTEXT",
        )
    return batch_rows


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


def execution_api_scope(snapshot: ExecutionSnapshot) -> OdooApiScope:
    """Derive the exact native API capability from one frozen preview."""

    write_fields: dict[str, set[str]] = {}
    read_fields: dict[str, set[str]] = {}
    lookup_fields: dict[str, set[str]] = {}

    def register_nested_lookup(value: Any) -> None:
        if isinstance(value, BusinessReference):
            fields = (*value.target_fields, *value.target_scope_fields)
            if fields:
                lookup_fields.setdefault(value.model, set()).update(fields)
            for component in (*value.key, *value.scope):
                register_nested_lookup(component)
        elif isinstance(value, tuple):
            for component in value:
                register_nested_lookup(component)

    row_by_id = {row.row_id: row for row in snapshot.rows}
    for dataset in snapshot.datasets:
        lookup_fields.setdefault(dataset.target_model, set()).update(
            (*dataset.identity_fields, *dataset.scope_fields)
        )
    for row in snapshot.rows:
        # Nested identity reads need their exact frozen parent capabilities,
        # including parents that are reference-only datasets with no writes.
        for component in (*row.business_identity, *row.business_scope):
            register_nested_lookup(component)
        if row.disposition not in {"CREATE", "UPDATE"}:
            continue
        for intent in row.fields:
            if intent.action not in {"OMIT", "EXPECT_PROTECTED"}:
                write_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.action != "OMIT":
                read_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.kind == "relation" and intent.action != "EXPECT_PROTECTED":
                register_nested_lookup(intent.value)
                lookup_fields.setdefault(intent.related_model, set()).update(
                    (
                        *intent.related_identity_fields,
                        *intent.related_scope_fields,
                    )
                )
                if intent.incoming_projection_field:
                    for dependency_row_id in intent.dependency_row_ids:
                        dependency = row_by_id[dependency_row_id]
                        read_fields.setdefault(
                            dependency.target_model,
                            set(),
                        ).add(intent.incoming_projection_field)
    model_names = sorted(set(write_fields) | set(read_fields) | set(lookup_fields))
    return OdooApiScope(
        preview_hash=snapshot.semantic_hash,
        # This transient permission check is derived from the frozen rows. It
        # does not change historical API-scope hashes used during recovery.
        creatable_models=tuple(sorted({row.target_model for row in snapshot.rows
            if row.disposition == "CREATE" and write_fields.get(row.target_model)})),
        models=tuple(
            OdooModelScope(
                model=model,
                write_fields=tuple(sorted(write_fields.get(model, set()))),
                read_fields=tuple(sorted(read_fields.get(model, set()))),
                lookup_fields=tuple(sorted(lookup_fields.get(model, set()))),
            )
            for model in model_names
            if write_fields.get(model)
            or read_fields.get(model)
            or lookup_fields.get(model)
        )
    )


def _execution_snapshot_error(
    workspace_state: WorkspaceState,
    snapshot: ExecutionSnapshot,
    *,
    operation: OdooOperation = OdooOperation.WRITE,
) -> str:
    """Explain an execution-shape problem before the user can press Load."""

    if workspace_state.odoo_connection_mode not in {
        OdooConnectionMode.LOCAL,
        OdooConnectionMode.REMOTE,
    }:
        return "Configure the exact Odoo load target first"
    if not assess_odoo_operation(snapshot.target_odoo_version, operation).allowed:
        return "This Odoo version is not enabled for the schema-bound load path"
    plan = snapshot.relationship_plan
    scheduled_rows = tuple(
        sorted(
            (row for row in snapshot.rows if row.schedule_ordinal >= 0),
            key=lambda row: row.schedule_ordinal,
        )
    )
    component_row_ids = tuple(
        row_id
        for component in plan.components
        for row_id in component.row_ids
    )
    schedule_component_by_row = {
        row_id: component.sequence
        for component in plan.components
        for row_id in component.row_ids
    }
    blocked_row_ids = {item.row_id for item in plan.blockers}
    expected_scheduled_ids = {
        row.row_id
        for row in snapshot.rows
        if row.disposition in {"CREATE", "UPDATE"}
        and row.row_id not in blocked_row_ids
    }
    completion_fields = {
        (item.row_id, item.field) for item in plan.completions
    }
    deferred_fields = {
        (row.row_id, intent.field)
        for row in snapshot.rows
        for intent in row.fields
        if intent.defer_on_create
    }
    if (
        plan.contract_version != 1
        or not _SHA256.fullmatch(plan.root_hash)
        or tuple(component.sequence for component in plan.components)
        != tuple(range(plan.component_count))
        or tuple(row.row_id for row in scheduled_rows) != component_row_ids
        or {row.row_id for row in scheduled_rows} != expected_scheduled_ids
        or tuple(row.schedule_ordinal for row in scheduled_rows)
        != tuple(range(len(scheduled_rows)))
        or any(
            row.schedule_component
            != schedule_component_by_row.get(row.row_id, -1)
            for row in scheduled_rows
        )
        or completion_fields != deferred_fields
    ):
        return (
            "The reviewed relationship schedule changed. Compare with Odoo "
            "again before loading."
        )
    if plan.blockers:
        blocker = plan.blockers[0]
        if blocker.code == "HARD_DEPENDENCY_CYCLE":
            return (
                "Required create-time relationships form a row cycle. Revise "
                "those relationships before loading."
            )
        return (
            "A reviewed relationship dependency cannot be scheduled safely. "
            "Resolve the blocked imported relationship before loading."
        )
    sequenced_datasets = tuple(
        sorted(snapshot.datasets, key=lambda item: item.sequence)
    )
    try:
        dependency_ordered = dependency_ordered_execution_datasets(
            sequenced_datasets
        )
    except ValueError:
        return (
            "The reviewed dataset dependency order is invalid. Compare with "
            "Odoo again before loading."
        )
    if tuple(dataset.dataset for dataset in sequenced_datasets) != tuple(
        dataset.dataset for dataset in dependency_ordered
    ):
        return (
            "The reviewed dataset order is stale. Compare with Odoo again so "
            "dependencies are loaded first."
        )
    write_rows = tuple(row for row in snapshot.rows if row.fields)
    if not write_rows:
        return NO_WRITE_ROWS_MESSAGE
    precision_issues = TargetNumericPrecisionLosses()
    digits_by_dataset = {
        dataset.dataset: dict(getattr(dataset, "field_digits", ()))
        for dataset in snapshot.datasets
    }
    for row in write_rows:
        field_digits = digits_by_dataset.get(row.dataset, {})
        for intent in row.fields:
            digits = field_digits.get(intent.field)
            if (
                digits is None
                or intent.kind != "scalar"
                or intent.action != "SET_VALUE"
                or type(intent.value) is bool
            ):
                continue
            precision_issues.add(row.target_model, intent.field, digits, intent.value)
    precision_message = precision_issues.first_message()
    if precision_message is not None:
        return precision_message
    datasets = {item.dataset: item for item in snapshot.datasets}
    rows_by_source = {
        (row.dataset, _portable_key(row.source_identity)): row
        for row in snapshot.rows
    }

    def incoming_reference_error(
        value: LogicalReference,
        intent: FieldIntent,
        dataset: ExecutionDataset,
        *,
        require_created: bool = True,
    ) -> str:
        if value.origin != "incoming":
            return (
                f"{dataset.dataset}.{intent.field} has no unique reviewed "
                "Odoo relationship match"
            )
        if value.dataset is None:
            return (
                f"{dataset.dataset}.{intent.field} has an incomplete imported "
                "relationship reference"
            )
        related_dataset = datasets.get(value.dataset)
        if (
            related_dataset is None
            or value.dataset not in dataset.dependencies
        ):
            return (
                f"{dataset.dataset}.{intent.field} must reference a reviewed "
                "dependency dataset"
            )
        referenced = rows_by_source.get(
            (value.dataset, _portable_key(value.key))
        )
        if referenced is None or (
            referenced.target_model != intent.related_model
            and not intent.incoming_projection_field
        ) or (
            referenced.target_model == intent.related_model
            and intent.incoming_projection_field
        ):
            return (
                f"{dataset.dataset}.{intent.field} has no reviewed imported record"
            )
        if referenced.row_id not in intent.dependency_row_ids:
            return (
                f"{dataset.dataset}.{intent.field} is missing its reviewed row "
                "dependency"
            )
        if referenced.disposition not in {
            "CREATE",
            "UPDATE",
            "UNCHANGED",
        }:
            return (
                f"{dataset.dataset}.{intent.field} has no usable imported record"
            )
        if (
            require_created
            and referenced.disposition == "CREATE"
            and not referenced.proposed_external_id
            and not intent.incoming_projection_field
        ):
            return f"{dataset.dataset}.{intent.field} has no imported create record"
        return ""

    for row in write_rows:
        dataset = datasets.get(row.dataset)
        if dataset is None:
            return f"Dataset {row.dataset} is missing from the reviewed load preview"
        if (
            workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
            and row.disposition == "UPDATE"
        ):
            for intent in row.fields:
                if (
                    intent.action == "OMIT"
                    or intent.kind == "scalar"
                    or intent.action == "SET_NULL"
                ):
                    continue
                value = intent.value
                if (
                    intent.action != "SET_VALUE"
                    or intent.relation_operation != "replace"
                ):
                    return (
                        "The remote-update slice supports exact relationship "
                        f"replacement; {row.dataset}.{intent.field} is outside it"
                    )
                references = value if isinstance(value, tuple) else (value,)
                for reference in references:
                    if isinstance(reference, LogicalReference):
                        error = incoming_reference_error(
                            reference,
                            intent,
                            dataset,
                            require_created=False,
                        )
                        if error:
                            return error
                    elif isinstance(reference, BusinessReference):
                        if reference.model != intent.related_model:
                            return (
                                f"{row.dataset}.{intent.field} targets another "
                                "Odoo model"
                            )
                    else:
                        return (
                            f"{row.dataset}.{intent.field} is not expressed by a "
                            "reviewed business key"
                        )
        if (
            workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE
            and row.disposition == "CREATE"
        ):
            if not row.proposed_external_id:
                return f"Dataset {row.dataset} has a create without an External ID"
            for intent in row.fields:
                if intent.action in {"OMIT", "SET_NULL", "EXPECT_PROTECTED"}:
                    continue
                if intent.kind == "scalar":
                    continue
                if (
                    intent.action == "SET_PROTECTED"
                    and intent.relation_operation == "replace"
                ):
                    continue
                value = intent.value
                if isinstance(value, tuple):
                    if (
                        intent.action != "SET_VALUE"
                        or intent.relation_operation not in {"replace", "add"}
                    ):
                        return (
                            "The remote-import slice supports replace/add for "
                            f"many2many creates; {row.dataset}.{intent.field} is "
                            "outside it"
                        )
                    for item in value:
                        if isinstance(item, LogicalReference):
                            error = incoming_reference_error(
                                item,
                                intent,
                                dataset,
                                require_created=False,
                            )
                            if error:
                                return error
                        elif isinstance(item, BusinessReference):
                            if item.model != intent.related_model:
                                return (
                                    f"{row.dataset}.{intent.field} targets another "
                                    "Odoo model"
                                )
                        else:
                            return (
                                f"{row.dataset}.{intent.field} contains an "
                                "unsupported relationship value"
                            )
                    continue
                if (
                    intent.action != "SET_VALUE"
                    or intent.relation_operation != "replace"
                ):
                    return (
                        "The remote-import slice supports only one reviewed "
                        f"many2one create; {row.dataset}.{intent.field} is outside it"
                    )
                if isinstance(value, LogicalReference):
                    error = incoming_reference_error(value, intent, dataset)
                    if error:
                        return error
                elif isinstance(value, BusinessReference):
                    if value.model != intent.related_model:
                        return (
                            f"{row.dataset}.{intent.field} targets another Odoo model"
                        )
                else:
                    return (
                        f"{row.dataset}.{intent.field} is not expressed by a "
                        "reviewed business key"
                    )
        for intent in row.fields:
            if intent.kind == "scalar" or intent.action != "SET_VALUE":
                continue
            if isinstance(intent.value, tuple):
                if intent.relation_operation not in {"replace", "add", "remove"}:
                    return (
                        f"{row.dataset}.{intent.field} uses unsupported relationship "
                        f"operation {intent.relation_operation}"
                    )
                if not all(
                    isinstance(item, BusinessReference | LogicalReference)
                    for item in intent.value
                ):
                    return (
                        f"{row.dataset}.{intent.field} contains an unsupported "
                        "relationship value"
                    )
            elif not isinstance(
                intent.value,
                BusinessReference | LogicalReference,
            ):
                return (
                    f"{row.dataset}.{intent.field} is not expressed by a reviewed "
                    "business key"
                )
    return ""


def _execution_scope_error_code(message: str) -> str:
    """Return one stable support code carried by a controlled scope message."""

    marker = "Support code: "
    if marker not in message:
        return ""
    code = message.rsplit(marker, 1)[1].strip().removesuffix(".")
    return code if code.replace("_", "").isalnum() else ""


def _execution_dependency_summary(
    snapshot: ExecutionSnapshot,
) -> ExecutionDependencySummary:
    """Describe the first frozen load groups without copying the planner."""

    row_by_id = {row.row_id: row for row in snapshot.rows}
    dataset_rank = {
        item.dataset: item.sequence for item in snapshot.datasets
    }
    groups: list[ExecutionLoadGroupPreview] = []
    components = tuple(
        sorted(
            snapshot.relationship_plan.components,
            key=lambda item: item.sequence,
        )
    )
    for component in components[:MAX_VISIBLE_LOAD_GROUPS]:
        datasets = tuple(
            sorted(
                {
                    row_by_id[row_id].dataset
                    for row_id in component.row_ids
                    if row_id in row_by_id
                },
                key=lambda value: (dataset_rank.get(value, 10**9), value),
            )
        )
        visible_datasets = datasets[:MAX_VISIBLE_GROUP_DATASETS]
        groups.append(
            ExecutionLoadGroupPreview(
                number=component.sequence + 1,
                record_count=len(component.row_ids),
                dataset_labels=tuple(
                    _execution_dataset_label(value)
                    for value in visible_datasets
                ),
                omitted_dataset_count=max(
                    0,
                    len(datasets) - len(visible_datasets),
                ),
            )
        )
    completion_rows = {
        item.row_id for item in snapshot.relationship_plan.completions
    }
    return ExecutionDependencySummary(
        groups=tuple(groups),
        total_group_count=len(components),
        omitted_group_count=max(0, len(components) - len(groups)),
        relationship_record_count=len(completion_rows),
        relationship_field_count=snapshot.relationship_plan.completion_count,
        relationship_link_count=snapshot.relationship_plan.edge_count,
    )


_BLOCKER_GUIDANCE: dict[str, tuple[str, str]] = {
    "HARD_DEPENDENCY_CYCLE": (
        "Required relationships cannot be created in a safe order",
        ("Make one relationship optional, point it to an existing Odoo record, "
        "or correct the source relationships, then compare again."),
    ),
    "MISSING_INCOMING_ROW": (
        "A related source record is missing",
        ("Add the supporting record or change the relationship to a valid "
        "reviewed record, then compare again."),
    ),
    "DUPLICATE_INCOMING_ROW": (
        "A related source record is not unique",
        ("Remove the duplicate or choose a business key that identifies one "
        "record, then compare again."),
    ),
    "INCOMPLETE_INCOMING_REFERENCE": (
        "A source relationship is incomplete",
        "Complete or remove the relationship in Match data, then compare again.",
    ),
    "INCOMING_MODEL_MISMATCH": (
        "A relationship points to the wrong Odoo record type",
        "Correct the relationship mapping and compare again.",
    ),
    "UNUSABLE_INCOMING_ROW": (
        "A supporting source record also needs attention",
        "Resolve the supporting record first, then compare again.",
    ),
    "MISSING_DEPENDENCY_STRENGTH": (
        "A relationship rule is incomplete",
        ("Review whether the relationship is required or optional, then compare "
        "again."),
    ),
    "BLOCKED_DEPENDENCY": (
        "A record depends on another blocked source record",
        ("Resolve the earlier relationship warning; Impodo will then recalculate "
        "the load order."),
    ),
    "AMBIGUOUS_ROWS": (
        "Some records match more than one Odoo record",
        "Choose a unique match in the final review, then compare again.",
    ),
    "BLOCKED_ROWS": (
        "Some records still need a valid mapping or value",
        ("Open the affected rows in Final review to see the field and reason. "
        "Correct the source, mapping, or Odoo record, then compare again."),
    ),
}


def _execution_blocker_summary(
    snapshot: ExecutionSnapshot,
) -> ExecutionBlockerSummary:
    """Group equivalent blocker evidence into bounded actionable messages."""

    row_by_id = {row.row_id: row for row in snapshot.rows}
    blockers_by_code: dict[str, set[str]] = {}
    for blocker in snapshot.relationship_plan.blockers:
        blockers_by_code.setdefault(blocker.code, set()).add(blocker.row_id)
    for disposition, code in (
        ("AMBIGUOUS", "AMBIGUOUS_ROWS"),
        ("BLOCKED", "BLOCKED_ROWS"),
    ):
        matching = {
            row.row_id
            for row in snapshot.rows
            if row.disposition == disposition
        }
        if matching:
            blockers_by_code[code] = matching

    groups: list[ExecutionBlockerGroup] = []
    for code, row_ids in blockers_by_code.items():
        title, action = _BLOCKER_GUIDANCE.get(
            code,
            (
                "A reviewed relationship cannot be loaded safely",
                "Review the related source value and mapping, then compare again.",
            ),
        )
        datasets = tuple(
            sorted(
                {
                    row_by_id[row_id].dataset
                    for row_id in row_ids
                    if row_id in row_by_id
                }
            )
        )
        visible_datasets = datasets[:MAX_VISIBLE_BLOCKER_DATASETS]
        groups.append(
            ExecutionBlockerGroup(
                code=code,
                title=title,
                action=action,
                record_count=len(row_ids),
                dataset_labels=tuple(
                    _execution_dataset_label(value)
                    for value in visible_datasets
                ),
                omitted_dataset_count=max(
                    0,
                    len(datasets) - len(visible_datasets),
                ),
            )
        )
    visible = tuple(groups[:MAX_VISIBLE_BLOCKER_GROUPS])
    return ExecutionBlockerSummary(
        groups=visible,
        total_group_count=len(groups),
        omitted_group_count=max(0, len(groups) - len(visible)),
    )


def _execution_dataset_label(value: str) -> str:
    return value.replace("_", " ").strip().title() or "Prepared records"


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


def _odoo_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError(
            "A relational business key cannot be used as a scalar",
            load_failure_code="LOAD_A_RELATIONAL_BUSINESS_KEY_CANNOT_BE_USED_AS_A_SCALAR",
        )
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError(
        "A prepared value is not supported by the Odoo API",
        load_failure_code="LOAD_A_PREPARED_VALUE_IS_NOT_SUPPORTED_BY_THE_ODOO_API",
    )


def _odoo_import_scalar(value: Any) -> str:
    """Render one scalar for Odoo's text-only ``Model.load`` matrix."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError(
            "A relational business key cannot be used as a scalar",
            load_failure_code="LOAD_A_RELATIONAL_BUSINESS_KEY_CANNOT_BE_USED_AS_A_SCALAR",
        )
    if value is None:
        return ""
    if type(value) is bool:
        return "1" if value else "0"
    if type(value) in {str, int, float}:
        return str(value)
    raise WorkspaceError(
        "A prepared value is not supported by the Odoo API",
        load_failure_code="LOAD_A_PREPARED_VALUE_IS_NOT_SUPPORTED_BY_THE_ODOO_API",
    )


def _portable_key(value: tuple[Any, ...]) -> str:
    return canonical_json_text(portable_value(value))


def _identity_cache_key(row: ExecutionRow) -> str:
    return canonical_json_text(
        {
            "model": row.target_model,
            "identity": portable_value(row.business_identity),
            "scope": portable_value(row.business_scope),
        }
    )


def _domain_cache_key(
    model: str,
    domain: tuple[tuple[str, str, Any], ...],
) -> str:
    return canonical_json_text({"model": model, "domain": domain})


def _write_diagnostic(
    error: OdooWriteError,
    row: ExecutionRow,
    batch_size: int = 1,
    *,
    method: str | None = None,
) -> OdooWriteDiagnostic:
    """Always retain operation context, even when Odoo supplies no useful detail."""

    return error.diagnostic or OdooWriteDiagnostic(
        code=(
            WriteFailureCode.UNKNOWN
            if isinstance(error, OdooWriteOutcomeUnknown)
            else WriteFailureCode.REJECTED
        ),
        model=row.target_model,
        method=method or ("create" if row.disposition == "CREATE" else "write"),
        batch_size=batch_size,
    )
