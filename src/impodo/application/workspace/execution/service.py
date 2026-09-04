"""Orchestrate one confirmed schema-bound load from the current snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
    MAX_CREATE_BATCH_ROWS,
    ProjectedOdooReceipt,
)
from impodo.domain.execution.dependency_scheduler import dependency_component_pages
from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    dependency_ordered_execution_datasets,
)
from impodo.domain.shared.models import (
    BusinessReference,
    LogicalReference,
    OdooReadIdentity,
    OdooWriteIdentity,
    canonical_json_text,
    portable_value,
    target_record_binding_hash,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    MAX_IDENTITY_LOOKUP_KEYS,
    MAX_PROJECTED_RECEIPT_IDS,
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.reconciliation import (
    ReconciliationRowStatus,
    ReconciliationRun,
)
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    transfer_destination_workspace,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.preflight_service import PreflightService


DEFAULT_CREATE_BATCH_ROWS = 10
NO_WRITE_ROWS_MESSAGE = "This preview has no rows to create or update"
MAX_VISIBLE_LOAD_GROUPS = 5
MAX_VISIBLE_GROUP_DATASETS = 3
MAX_VISIBLE_BLOCKER_GROUPS = 5
MAX_VISIBLE_BLOCKER_DATASETS = 3
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ReadCredentialBindingProvider = Callable[[WorkspaceState], str]


class ExecutionWorkspaceRepository(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class ExecutionJournalRepository(Protocol):
    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        transfer_preflight_hash: str = "",
    ) -> None: ...

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None: ...

    def record_batch_started(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None: ...

    def record_recovery(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        actor: Actor,
    ) -> None: ...

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun: ...

    def get_current_run(
        self,
        workspace_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None: ...

    def get_run(self, workspace_id: str, run_id: str) -> ExecutionRun | None: ...


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
    credential_refresh_required: bool = False
    dependency_summary: ExecutionDependencySummary = field(
        default_factory=ExecutionDependencySummary
    )
    blocker_summary: ExecutionBlockerSummary = field(
        default_factory=ExecutionBlockerSummary
    )

    @property
    def can_load(self) -> bool:
        return (
            self.snapshot.write_count > 0
            and int(self.snapshot.counts.get("BLOCKED", 0)) == 0
            and int(self.snapshot.counts.get("AMBIGUOUS", 0)) == 0
            and self.current_run is None
            and not self.scope_error
        )

    @property
    def can_complete_without_load(self) -> bool:
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


class ExecutionService:
    """Validate, journal, and execute a reviewed disposable-target load."""

    def __init__(
        self,
        workspaces: ExecutionWorkspaceRepository,
        preflight: PreflightService,
        journal: ExecutionJournalRepository,
        authorization: AuthorizationPolicy,
        *,
        require_remote_read_identity: bool = False,
        require_remote_write_identity: bool = False,
        current_read_credential_binding: (
            ReadCredentialBindingProvider | None
        ) = None,
    ) -> None:
        self.workspaces = workspaces
        self.preflight = preflight
        self.journal = journal
        self.authorization = authorization
        self.require_remote_read_identity = require_remote_read_identity
        self.require_remote_write_identity = require_remote_write_identity
        self.current_read_credential_binding = current_read_credential_binding

    def current_preview(self, workspace_id: str) -> ExecutionPreview | None:
        snapshot = self.preflight.current_execution_snapshot(workspace_id)
        if snapshot is None:
            return None
        workspace_state = self.workspaces.get(workspace_id)
        current_read_credential_binding = (
            self.current_read_credential_binding(workspace_state)
            if self.current_read_credential_binding is not None
            else None
        )
        current = self.journal.get_current_run(workspace_id, snapshot.semantic_hash)
        api_scope = execution_api_scope(snapshot)
        credential_error = _read_credential_snapshot_error(
            workspace_state,
            snapshot,
            current_read_credential_binding=current_read_credential_binding,
        )
        return ExecutionPreview(
            snapshot=snapshot,
            datasets=tuple(
                ExecutionDatasetPreview(
                    dataset=dataset.dataset,
                    target_model=dataset.target_model,
                    create_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "CREATE"
                        for row in snapshot.rows
                    ),
                    update_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "UPDATE"
                        for row in snapshot.rows
                    ),
                    unchanged_count=sum(
                        row.dataset == dataset.dataset
                        and row.disposition == "UNCHANGED"
                        for row in snapshot.rows
                    ),
                )
                for dataset in snapshot.datasets
                if any(row.dataset == dataset.dataset for row in snapshot.rows)
            ),
            current_run=current,
            api_scope=api_scope,
            deferred_create_count=_planned_deferred_create_count(
                snapshot,
                create_batch_rows=DEFAULT_CREATE_BATCH_ROWS,
            ),
            scope_error=(
                credential_error
                or _execution_snapshot_error(
                    workspace_state,
                    snapshot,
                )
            ),
            credential_refresh_required=bool(credential_error),
            dependency_summary=_execution_dependency_summary(snapshot),
            blocker_summary=_execution_blocker_summary(snapshot),
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
    ) -> ExecutionRun:
        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.source_mode is SourceMode.ODOO:
            raise WorkspaceError(
                "Use the confirmed Stage 8B transfer route for an Odoo-to-Odoo load."
            )
        create_batch_rows = validated_create_batch_rows(batch_rows)
        preview = self.current_preview(workspace_id)
        if preview is None:
            raise WorkspaceError("Compare the prepared data with Odoo first")
        snapshot = preview.snapshot
        if snapshot.semantic_hash != expected_snapshot_hash:
            raise WorkspaceError("The load preview changed. Review it again.")
        if preview.scope_error:
            raise WorkspaceError(preview.scope_error)
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
        started_at = datetime.now(timezone.utc)
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
        self.journal.start_run(workspace_id, run, actor=actor)
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
        progress: Callable[[ExecutionRun], None] | None = None,
    ) -> ExecutionRun:
        """Enter the shared writer from a current, confirmed Stage 8B snapshot."""

        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            workspace_id=workspace_id,
        )
        source_workspace = self.workspaces.get(workspace_id)
        if source_workspace.source_mode is not SourceMode.ODOO:
            raise WorkspaceError("Stage 8B requires a frozen Odoo source")
        report = source_workspace.transfer_preflight_report
        if (
            report is None
            or not report.ready
            or report.content_hash != expected_preflight_hash
            or snapshot.preflight_result_hash != report.content_hash
            or snapshot.workspace_id != workspace_id
            or snapshot.semantic_hash != expected_snapshot_hash
        ):
            raise WorkspaceError(
                "The destination preflight changed. Run it again before loading."
            )
        destination_workspace = transfer_destination_workspace(source_workspace)
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
                "This approved transfer already has a load journal. Verify its outcome."
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
                "The destination transfer key changed principal or company context"
            )

        write_rows = tuple(
            row for row in snapshot.rows if row.disposition in {"CREATE", "UPDATE"}
        )
        if not write_rows:
            raise WorkspaceError("This approved transfer has no records to load")
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
            started_at=datetime.now(timezone.utc),
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
    ) -> ExecutionRun:
        """Resume an interrupted journal only from exact read-back evidence."""

        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.source_mode is SourceMode.ODOO:
            raise WorkspaceError(
                "Use the Stage 8B transfer recovery action for an interrupted "
                "Odoo-to-Odoo load."
            )
        snapshot = self.preflight.current_execution_snapshot(workspace_id)
        if snapshot is None:
            raise WorkspaceError("The interrupted load preview is no longer current")
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
            raise WorkspaceError("The interrupted load or preview is no longer current")
        if executor.target_hash != snapshot.target_hash:
            raise WorkspaceError(
                "The recovery writer points to a different Odoo target"
            )
        if executor.scope_hash != execution_api_scope(snapshot).semantic_hash:
            raise WorkspaceError(
                "The recovery writer is not bound to this reviewed load preview"
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
        already_applied = (
            not run.in_flight_count
            and all(
                item.recovery_hash == recovery.semantic_hash
                for item in run.rows
            )
        )
        if not already_applied:
            recovered = self._classify_recovery(snapshot, run, recovery)
            self.journal.record_recovery(
                workspace_id,
                run.run_id,
                recovered,
                actor=actor,
            )
            run = self.journal.get_run(workspace_id, run.run_id)
            if run is None:
                raise WorkspaceError("The recovered load journal could not be reloaded")

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
        progress: Callable[[ExecutionRun], None] | None = None,
    ) -> ExecutionRun:
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
            or not report.ready
            or report.content_hash != expected_preflight_hash
            or snapshot.workspace_id != workspace_id
            or snapshot.semantic_hash != expected_snapshot_hash
            or snapshot.preflight_result_hash != report.content_hash
        ):
            raise WorkspaceError(
                "The interrupted transfer preview or preflight is no longer current"
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
                "The interrupted transfer journal is no longer current"
            )

        destination_workspace = transfer_destination_workspace(source_workspace)
        preview = ExecutionPreview(
            snapshot=snapshot,
            datasets=(),
            current_run=run,
            api_scope=execution_api_scope(snapshot),
            deferred_create_count=0,
            scope_error=_execution_snapshot_error(destination_workspace, snapshot),
        )
        if preview.scope_error:
            raise WorkspaceError(preview.scope_error)
        if executor.target_hash != snapshot.target_hash:
            raise WorkspaceError(
                "The recovery writer points to a different Odoo target"
            )
        if executor.scope_hash != preview.api_scope.semantic_hash:
            raise WorkspaceError(
                "The recovery writer is not bound to this transfer preview"
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
                "The destination transfer key changed principal or company context"
            )
        if recovery.verification_credential_binding_hash != credential_binding_hash:
            raise WorkspaceError(
                "The recovery evidence was collected with another destination "
                "transfer key"
            )

        recovery = ReconciliationRun.from_json(recovery.to_json())
        already_applied = (
            not run.in_flight_count
            and all(item.recovery_hash == recovery.semantic_hash for item in run.rows)
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
                    "The recovered transfer journal could not be reloaded"
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
            progress=progress,
            create_with_external_ids=True,
        )

    def _continue_run(
        self,
        workspace_state: WorkspaceState,
        snapshot: ExecutionSnapshot,
        run: ExecutionRun,
        executor: OdooWriteExecutor,
        actor: Actor,
        *,
        identity_cache: dict[str, int],
        progress: Callable[[ExecutionRun], None] | None,
        create_with_external_ids: bool = False,
    ) -> ExecutionRun:
        """Consume unfinished rows from one durable schedule."""

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
        report_progress = progress or (lambda _run: None)
        report_progress(run)

        recorded: dict[str, ExecutionRowAttempt] = {
            item.row_id: item for item in run.rows
        }
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
            (
                (component.sequence, component.row_ids)
                for component in snapshot.relationship_plan.components
            )
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
                report_progress(replace(run, rows=tuple(recorded.values())))
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
                report_progress(replace(run, rows=tuple(recorded.values())))
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
            for start in range(0, len(creates), create_batch_rows):
                batch = creates[start : start + create_batch_rows]
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
                            import_relations=(
                                create_with_external_ids
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
                        self.journal.record_outcomes(
                            workspace_id, run.run_id, (outcome,)
                        )
                        recorded[row.row_id] = outcome
                        report_progress(replace(run, rows=tuple(recorded.values())))
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
                    batch_rows_for_transport = tuple(
                        item[0] for item in prepared_group
                    )
                    self._start_transport_batch(
                        workspace_id,
                        run.run_id,
                        batch_rows_for_transport,
                        recorded,
                        phase="CREATE",
                        component=page.component_sequence,
                        page=page.page_sequence,
                        batch=next_transport_batch,
                    )
                    next_transport_batch += 1
                    report_progress(replace(run, rows=tuple(recorded.values())))
                    try:
                        values = tuple(item[1] for item in prepared_group)
                        if (
                            create_with_external_ids
                            or workspace_state.odoo_connection_mode
                            is OdooConnectionMode.REMOTE
                        ):
                            identifiers = executor.load_create_rows(
                                dataset.target_model,
                                values,
                                tuple(item[0].proposed_external_id for item in prepared_group),
                            )
                        else:
                            identifiers = executor.create_rows(
                                dataset.target_model,
                                values,
                            )
                    except OdooWriteOutcomeUnknown as error:
                        outcomes = tuple(
                            replace(
                                recorded[row.row_id],
                                status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                                safe_error=str(error),
                            )
                            for row, _values, _deferred in prepared_group
                        )
                        self.journal.record_outcomes(
                            workspace_id, run.run_id, outcomes
                        )
                        recorded.update({item.row_id: item for item in outcomes})
                        report_progress(replace(run, rows=tuple(recorded.values())))
                        stop_after_unknown = True
                        break
                    except OdooWriteRejected as error:
                        outcomes = tuple(
                            replace(
                                recorded[row.row_id],
                                status=ExecutionRowStatus.FAILED,
                                safe_error=str(error),
                            )
                            for row, _values, _deferred in prepared_group
                        )
                        self.journal.record_outcomes(
                            workspace_id, run.run_id, outcomes
                        )
                        recorded.update({item.row_id: item for item in outcomes})
                        report_progress(replace(run, rows=tuple(recorded.values())))
                        stop_after_rejection = True
                        break
                    outcomes = []
                    for (row, _values, deferred), identifier in zip(
                        prepared_group, identifiers, strict=True
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
                    self.journal.record_outcomes(
                        workspace_id, run.run_id, outcomes
                    )
                    recorded.update({item.row_id: item for item in outcomes})
                    report_progress(replace(run, rows=tuple(recorded.values())))
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
                    report_progress(replace(run, rows=tuple(recorded.values())))
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
                    report_progress(replace(run, rows=tuple(recorded.values())))
                    try:
                        executor.update_row(row.target_model, record_id, values)
                    except OdooWriteOutcomeUnknown as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            safe_error=str(error),
                        )
                        stop_after_unknown = True
                    except OdooWriteRejected as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            safe_error=str(error),
                        )
                        stop_after_rejection = True
                    else:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.COMMITTED,
                            odoo_id=record_id,
                        )
                self.journal.record_outcomes(workspace_id, run.run_id, (outcome,))
                recorded[row.row_id] = outcome
                report_progress(replace(run, rows=tuple(recorded.values())))

        if not stop_after_unknown and not stop_after_rejection:
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
                progress=lambda: report_progress(
                    replace(run, rows=tuple(recorded.values()))
                ),
            )
            stop_after_unknown = completion_stopped
            report_progress(replace(run, rows=tuple(recorded.values())))

        remaining = tuple(
            row
            for row in write_rows
            if recorded[row.row_id].status
            in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }
        )
        if remaining:
            self._record_blocked(
                workspace_id,
                run.run_id,
                remaining,
                recorded,
                "Not attempted because an earlier dependency did not complete",
            )
            report_progress(replace(run, rows=tuple(recorded.values())))
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
        report_progress(completed_run)
        return completed_run

    @staticmethod
    def _classify_recovery(
        snapshot: ExecutionSnapshot,
        run: ExecutionRun,
        recovery: ReconciliationRun,
    ) -> tuple[ExecutionRowAttempt, ...]:
        """Turn one exact read-back into resumable journal states."""

        if (
            recovery.workspace_id != run.workspace_id
            or recovery.execution_run_id != run.run_id
            or recovery.snapshot_hash != run.snapshot_hash
            or recovery.target_hash != run.target_hash
            or recovery.target_database != run.target_database
        ):
            raise WorkspaceError("The recovery report belongs to another load")
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
                "The recovery report used another Odoo principal or context"
            )
        rows = {
            row.row_id: row
            for row in snapshot.rows
            if row.disposition in {"CREATE", "UPDATE"}
        }
        outcomes = {item.row_id: item for item in recovery.rows}
        attempts = {item.row_id: item for item in run.rows}
        projected_requirements = _projected_receipt_requirements(snapshot)
        if set(rows) != set(outcomes) or set(rows) != set(attempts):
            raise WorkspaceError("The recovery report does not cover every load row")

        recovered = []
        for row_id, attempt in attempts.items():
            row = rows[row_id]
            outcome = outcomes[row_id]
            if outcome.execution_status != attempt.status.value:
                raise WorkspaceError("The recovery report is stale")
            if outcome.target_model != attempt.target_model:
                raise WorkspaceError("The recovery report changed a target model")
            if outcome.operation != attempt.operation:
                raise WorkspaceError("The recovery report changed a write operation")

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
                        "A completed earlier component no longer matches Odoo"
                    )
            elif status in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }:
                if outcome.status is not ReconciliationRowStatus.NOT_WRITTEN:
                    raise WorkspaceError("An unstarted row has conflicting recovery evidence")
            elif status is ExecutionRowStatus.IN_FLIGHT:
                if outcome.status is ReconciliationRowStatus.VERIFIED:
                    if outcome.odoo_id is None:
                        raise WorkspaceError("Recovery found no durable Odoo receipt")
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
                        "The interrupted Odoo outcome is not safe to resume"
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
                        "The partial relationship result is not safe to resume"
                    )
            else:
                raise WorkspaceError(
                    "This stopped load cannot resume; run Check changes again"
                )
            recovered.append(
                replace(
                    attempt,
                    status=status,
                    odoo_id=odoo_id,
                    safe_error=safe_error,
                    recovery_hash=recovery.semantic_hash,
                )
            )
        return tuple(recovered)

    def complete_no_changes(
        self,
        workspace_id: str,
        *,
        expected_snapshot_hash: str,
        actor: Actor,
    ) -> ExecutionRun:
        """Record a reviewed zero-write comparison without contacting Odoo."""

        self.authorization.require(
            actor,
            Capability.EXPORT_PLAN_EXECUTE,
            workspace_id=workspace_id,
        )
        workspace_state = self.workspaces.get(workspace_id)
        if workspace_state.source_mode is SourceMode.ODOO:
            raise WorkspaceError(
                "Use the Stage 8B transfer outcome to complete an Odoo-to-Odoo load."
            )
        preview = self.current_preview(workspace_id)
        if preview is None:
            raise WorkspaceError("Compare the prepared data with Odoo first")
        snapshot = preview.snapshot
        if snapshot.semantic_hash != expected_snapshot_hash:
            raise WorkspaceError("The comparison changed. Review it again.")
        if preview.current_run is not None:
            if (
                preview.current_run.status is ExecutionRunStatus.COMPLETED
                and preview.current_run.total_count == 0
            ):
                return preview.current_run
            raise WorkspaceError(
                "This comparison already has a load result. Review that result first."
            )
        if preview.scope_error and preview.scope_error != NO_WRITE_ROWS_MESSAGE:
            raise WorkspaceError(preview.scope_error)
        if not preview.can_complete_without_load:
            raise WorkspaceError(
                "Only a complete comparison with no proposed Odoo changes can finish here"
            )
        started_at = datetime.now(timezone.utc)
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
        self.journal.start_run(workspace_id, run, actor=actor)
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
                    matches = executor.find_ids_many(model, page)
                    if len(matches) != len(page):
                        raise WorkspaceError(
                            "Odoo returned an incomplete create-key check"
                        )
                    if any(identifiers for identifiers in matches):
                        raise WorkspaceError(
                            "An Odoo business key marked for creation now exists; "
                            "run destination preflight again"
                        )
        except OdooWriteRejected as error:
            raise WorkspaceError(
                "Odoo create-key verification failed before loading"
            ) from error

    @staticmethod
    def _resolve_identity_crosswalk(
        write_rows: Sequence[ExecutionRow],
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        executor: OdooWriteExecutor,
    ) -> dict[str, int]:
        """Bulk-verify every existing target before journaling any write."""

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
                    "The reviewed Odoo identity bindings conflict"
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
                                "An incoming relationship is incomplete"
                            )
                        referenced = by_source.get(
                            (value.dataset, _portable_key(value.key))
                        )
                        if referenced is None:
                            raise WorkspaceError(
                                "A related prepared row could not be found"
                            )
                        if referenced.disposition != "CREATE":
                            add_row(referenced)
                        continue
                    if isinstance(value, BusinessReference):
                        key, scope = value.key, value.scope
                    elif isinstance(value, LogicalReference):
                        key, scope = value.key, value.scope
                    else:
                        raise WorkspaceError(
                            "A relationship is not expressed by a business key"
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
                        tuple(item["domain"] for item in page),
                    )
                    if len(matches) != len(page):
                        raise WorkspaceError(
                            "Odoo returned an incomplete identity crosswalk"
                        )
                    for requirement, identifiers in zip(
                        page,
                        matches,
                        strict=True,
                    ):
                        if len(identifiers) != 1:
                            raise WorkspaceError(
                                "The Odoo business key no longer matches exactly one record"
                            )
                        identifier = identifiers[0]
                        expected = requirement["expected"]
                        if expected and target_record_binding_hash(
                            model, identifier
                        ) != expected:
                            raise WorkspaceError(
                                "An Odoo business key now targets a different record; compare again"
                            )
                        for alias in requirement["aliases"]:
                            identity_cache[alias] = identifier
        except OdooWriteRejected as error:
            raise WorkspaceError(
                "Odoo identity verification failed before loading"
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
                        "A required Odoo create receipt was not journalled"
                    )
                if intent.incoming_projection_field and not any(
                    receipt.projection_field
                    == intent.incoming_projection_field
                    and receipt.target_model == intent.related_model
                    for receipt in attempt.projected_receipts
                ):
                    raise WorkspaceError(
                        "A generated Odoo relationship receipt was not journalled"
                    )

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
            raise WorkspaceError("Execution transport phase is invalid")
        attempts = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.IN_FLIGHT,
                attempt=recorded[row.row_id].attempt + 1,
                odoo_id=(known_ids or {}).get(row.row_id),
                safe_error="",
                schedule_component=component,
                transport_page=page,
                transport_batch=batch,
                transport_phase=phase,
            )
            for row in rows
        )
        self.journal.record_batch_started(
            workspace_id,
            run_id,
            attempts,
        )
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
            raise WorkspaceError("Configure the exact Odoo load target first")
        if preview.current_run is not None:
            raise WorkspaceError(
                "This preview was already loaded. Compare with Odoo again first."
            )
        if preview.scope_error:
            raise WorkspaceError(preview.scope_error)
        if not preview.can_load:
            raise WorkspaceError(
                "Resolve every blocked or ambiguous row before loading"
            )
        if executor.target_hash != snapshot.target_hash:
            raise WorkspaceError(
                "The load connection points to a different Odoo target"
            )
        if executor.scope_hash != preview.api_scope.semantic_hash:
            raise WorkspaceError(
                "The writer is not bound to this reviewed load preview"
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
                                "A generated relationship source was not created"
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
                        "Odoo generated-record read-back was incomplete"
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
                    "A generated relationship source journal is inconsistent"
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
                        "An Odoo generated-record receipt changed"
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
            self.journal.record_outcomes(workspace_id, run_id, outcomes)
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
    ) -> tuple[bool, int]:
        """Patch deferred create relationships after all first-pass creates."""

        for row in rows:
            intents = deferred_by_row.get(row.row_id)
            if not intents:
                continue
            attempt = recorded[row.row_id]
            if (
                attempt.status is not ExecutionRowStatus.PARTIALLY_APPLIED
                or attempt.odoo_id is None
            ):
                continue
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
                self.journal.record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
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
                known_ids={row.row_id: attempt.odoo_id},
            )
            next_transport_batch += 1
            attempt = recorded[row.row_id]
            progress()
            try:
                executor.update_row(row.target_model, attempt.odoo_id, values)
            except OdooWriteOutcomeUnknown as error:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                    safe_error=str(error),
                )
                self.journal.record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                progress()
                return True, next_transport_batch
            except OdooWriteRejected as error:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.PARTIALLY_APPLIED,
                    safe_error=(
                        "Record was created, but its deferred relationship "
                        f"update failed: {error}"
                    ),
                )
            else:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.COMMITTED,
                    safe_error="",
                )
            self.journal.record_outcomes(workspace_id, run_id, (outcome,))
            recorded[row.row_id] = outcome
            progress()
            if outcome.status is ExecutionRowStatus.PARTIALLY_APPLIED:
                return True, next_transport_batch
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
        import_relations: bool = False,
        skip_fields: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for intent in row.fields:
            if intent.action == "OMIT" or intent.field in skip_fields:
                continue
            if intent.action == "SET_NULL":
                values[intent.field] = "" if import_relations else None
            elif intent.kind == "scalar":
                values[intent.field] = (
                    _odoo_import_scalar(intent.value)
                    if import_relations
                    else _odoo_scalar(intent.value)
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
        if not values:
            raise WorkspaceError("A planned write has no permitted field values")
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
                    "many2many import slice"
                )
            for item in value:
                if isinstance(item, BusinessReference):
                    if item.model != intent.related_model:
                        raise WorkspaceError(
                            f"Relationship field {intent.field} targets another "
                            "Odoo model"
                        )
                elif not (
                    isinstance(item, LogicalReference)
                    and item.origin == "incoming"
                    and item.dataset is not None
                ):
                    raise WorkspaceError(
                        f"Relationship field {intent.field} is outside the remote "
                        "many2many import slice"
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
                "many2one import slice"
            )
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if not intent.incoming_projection_field:
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
                    f"Relationship field {intent.field} targets another Odoo model"
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
            "many2one import slice"
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
                "many2one import slice"
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
                "External ID"
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
                f"Relationship field {intent.field} has an unsupported operation"
            )
        if intent.relation_operation != "replace":
            raise WorkspaceError(
                f"Relationship field {intent.field} must use replace"
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
                raise WorkspaceError("An incoming relationship is incomplete")
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
                raise WorkspaceError("A related prepared row could not be found")
            if intent.incoming_projection_field:
                raise WorkspaceError(
                    "A generated Odoo relationship receipt was not journalled"
                )
            if referenced.disposition == "CREATE":
                raise WorkspaceError("A related Odoo create did not complete")
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
        if isinstance(value, BusinessReference):
            key, scope = value.key, value.scope
        elif isinstance(value, LogicalReference):
            key, scope = value.key, value.scope
        else:
            raise WorkspaceError("A relationship is not expressed by a business key")
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
            "The reviewed Odoo identity crosswalk is incomplete"
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
        self.journal.record_outcomes(workspace_id, run_id, outcomes)
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
        raise WorkspaceError("The Odoo business-key shape is incomplete")
    return tuple(
        (field, "=", _odoo_scalar(value))
        for field, value in zip(
            (*identity_fields, *scope_fields),
            (*identity, *scope),
            strict=True,
        )
    )


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
        raise WorkspaceError("Rows per Odoo batch must be a whole number")
    try:
        batch_rows = int(value)
    except (TypeError, ValueError) as error:
        raise WorkspaceError("Rows per Odoo batch must be a whole number") from error
    if str(value).strip() != str(batch_rows):
        raise WorkspaceError("Rows per Odoo batch must be a whole number")
    if batch_rows < 1 or batch_rows > MAX_CREATE_BATCH_ROWS:
        raise WorkspaceError(
            f"Rows per Odoo batch must be between 1 and {MAX_CREATE_BATCH_ROWS}"
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
                "Probe the separate remote write credential before loading"
            )
        if credential_binding_hash:
            raise WorkspaceError("The write credential has no principal evidence")
        return
    if not credential_binding_hash:
        raise WorkspaceError("The write principal has no credential-generation binding")
    expected_readable = tuple(item.model for item in preview.api_scope.models)
    expected_writable = tuple(
        item.model for item in preview.api_scope.models if item.write_fields
    )
    if identity.target_hash != preview.snapshot.target_hash:
        raise WorkspaceError("The write principal belongs to a different Odoo target")
    if identity.readable_models != expected_readable:
        raise WorkspaceError("The write principal read-back scope changed")
    if identity.writable_models != expected_writable:
        raise WorkspaceError("The write principal model scope changed")
    hashes = (
        credential_binding_hash,
        identity.principal_hash,
        identity.permission_hash,
        identity.context_hash,
    )
    if not all(_SHA256.fullmatch(value) for value in hashes):
        raise WorkspaceError("Execution write-identity evidence is invalid")


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
                "The interrupted load has no approved remote write identity"
            )
        if identity is not None or credential_binding_hash:
            raise WorkspaceError(
                "The local load cannot acquire a remote write identity during recovery"
            )
        return
    if identity is None:
        raise WorkspaceError(
            "Re-probe the approved write principal before resuming"
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
            "The write principal, permission, or context changed after interruption"
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
                "Re-probe the current Odoo read key before loading"
            )
        if credential_binding_hash:
            raise WorkspaceError("The read credential has no principal evidence")
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
        raise WorkspaceError("Execution read-identity evidence is incomplete")
    if (
        actual != expected
        or identity.target_hash != snapshot.target_hash
        or identity.readable_models != snapshot.readable_models
    ):
        raise WorkspaceError(
            "The Odoo read key, principal, permissions, or context changed; "
            "refresh the schema and compare again"
        )


def execution_api_scope(snapshot: ExecutionSnapshot) -> OdooApiScope:
    """Derive the exact native API capability from one frozen preview."""

    write_fields: dict[str, set[str]] = {}
    read_fields: dict[str, set[str]] = {}
    lookup_fields: dict[str, set[str]] = {}
    row_by_id = {row.row_id: row for row in snapshot.rows}
    for dataset in snapshot.datasets:
        lookup_fields.setdefault(dataset.target_model, set()).update(
            (*dataset.identity_fields, *dataset.scope_fields)
        )
    for row in snapshot.rows:
        if row.disposition not in {"CREATE", "UPDATE"}:
            continue
        for intent in row.fields:
            if intent.action != "OMIT":
                write_fields.setdefault(row.target_model, set()).add(intent.field)
                read_fields.setdefault(row.target_model, set()).add(intent.field)
            if intent.kind == "relation":
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
) -> str:
    """Explain an execution-shape problem before the user can press Load."""

    if workspace_state.odoo_connection_mode not in {
        OdooConnectionMode.LOCAL,
        OdooConnectionMode.REMOTE,
    }:
        return "Configure the exact Odoo load target first"
    if not snapshot.target_odoo_version.startswith("19."):
        return "The schema-bound load path requires Odoo 19"
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
                if intent.action == "OMIT":
                    continue
                if intent.kind == "scalar":
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
        "Make one relationship optional, point it to an existing Odoo record, "
        "or correct the source relationships, then compare again.",
    ),
    "MISSING_INCOMING_ROW": (
        "A related source record is missing",
        "Add the supporting record or change the relationship to a valid "
        "reviewed record, then compare again.",
    ),
    "DUPLICATE_INCOMING_ROW": (
        "A related source record is not unique",
        "Remove the duplicate or choose a business key that identifies one "
        "record, then compare again.",
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
        "Review whether the relationship is required or optional, then compare "
        "again.",
    ),
    "BLOCKED_DEPENDENCY": (
        "A record depends on another blocked source record",
        "Resolve the earlier relationship warning; Impodo will then recalculate "
        "the load order.",
    ),
    "AMBIGUOUS_ROWS": (
        "Some records match more than one Odoo record",
        "Choose a unique match in the final review, then compare again.",
    ),
    "BLOCKED_ROWS": (
        "Some records still need a valid mapping or value",
        "Resolve those rows in the final review, then compare again.",
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
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError("A relational business key cannot be used as a scalar")
    if value is None or type(value) in {str, int, bool, float}:
        return value
    raise WorkspaceError("A prepared value is not supported by the Odoo API")


def _odoo_import_scalar(value: Any) -> str:
    """Render one scalar for Odoo's text-only ``Model.load`` matrix."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BusinessReference | LogicalReference):
        raise WorkspaceError("A relational business key cannot be used as a scalar")
    if value is None:
        return ""
    if type(value) is bool:
        return "1" if value else "0"
    if type(value) in {str, int, float}:
        return str(value)
    raise WorkspaceError("A prepared value is not supported by the Odoo API")


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
