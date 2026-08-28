"""Orchestrate one confirmed schema-bound load from the current snapshot."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
)
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
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import (
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, SourceMode
from impodo.domain.workspace.errors import WorkspaceError
from impodo.application.preflight_service import PreflightService


DEFAULT_CREATE_BATCH_ROWS = 10
NO_WRITE_ROWS_MESSAGE = "This preview has no rows to create or update"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ReadCredentialBindingProvider = Callable[[WorkspaceState], str]


class ExecutionWorkspaceRepository(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


class ExecutionJournalRepository(Protocol):
    def start_run(
        self, workspace_id: str, run: ExecutionRun, *, actor: Actor
    ) -> None: ...

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
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


@dataclass(frozen=True, slots=True)
class ExecutionDatasetPreview:
    dataset: str
    target_model: str
    create_count: int
    update_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class ExecutionPreview:
    snapshot: ExecutionSnapshot
    datasets: tuple[ExecutionDatasetPreview, ...]
    current_run: ExecutionRun | None
    api_scope: OdooApiScope
    deferred_create_count: int
    scope_error: str = ""
    credential_refresh_required: bool = False

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
                "Pinned Odoo loading is not available yet. No Odoo record was changed."
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
        attempts = tuple(
            ExecutionRowAttempt(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                target_model=row.target_model,
                operation=row.disposition,
                field_names=tuple(intent.field for intent in row.fields),
                proposed_external_id=row.proposed_external_id,
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
        report_progress = progress or (lambda _run: None)
        report_progress(run)

        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, _portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        identity_cache: dict[str, int] = {}
        source_cache: dict[tuple[str, str], int] = {}
        recorded: dict[str, ExecutionRowAttempt] = {
            item.row_id: item for item in attempts
        }
        deferred_by_row: dict[str, tuple[FieldIntent, ...]] = {}
        stop_after_unknown = False
        for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence):
            dataset_rows = tuple(
                row
                for row in write_rows
                if row.dataset == dataset.dataset
                and row.row_id not in {
                    row_id
                    for row_id, attempt in recorded.items()
                    if attempt.status is not ExecutionRowStatus.PLANNED
                }
            )
            if stop_after_unknown:
                self._record_blocked(
                    workspace_id,
                    run.run_id,
                    dataset_rows,
                    recorded,
                    "Not attempted after an uncertain Odoo response",
                )
                report_progress(replace(run, rows=tuple(recorded.values())))
                continue
            creates = tuple(row for row in dataset_rows if row.disposition == "CREATE")
            updates = tuple(row for row in dataset_rows if row.disposition == "UPDATE")
            for start in range(0, len(creates), create_batch_rows):
                batch = creates[start : start + create_batch_rows]
                prepared_rows: list[
                    tuple[ExecutionRow, dict[str, Any], tuple[FieldIntent, ...]]
                ] = []
                for row in batch:
                    try:
                        deferred = self._deferred_create_intents(
                            row,
                            by_source,
                            source_cache,
                        )
                        values = self._row_values(
                            row,
                            metadata,
                            by_source,
                            source_cache,
                            identity_cache,
                            executor,
                            import_relations=(
                                workspace_state.odoo_connection_mode
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
                    try:
                        values = tuple(item[1] for item in prepared_group)
                        if workspace_state.odoo_connection_mode is OdooConnectionMode.REMOTE:
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
                                attempt=1,
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
                                attempt=1,
                                safe_error=str(error),
                            )
                            for row, _values, _deferred in prepared_group
                        )
                        self.journal.record_outcomes(
                            workspace_id, run.run_id, outcomes
                        )
                        recorded.update({item.row_id: item for item in outcomes})
                        report_progress(replace(run, rows=tuple(recorded.values())))
                        continue
                    outcomes = []
                    for (row, _values, deferred), identifier in zip(
                        prepared_group, identifiers, strict=True
                    ):
                        outcome = replace(
                            recorded[row.row_id],
                            status=(
                                ExecutionRowStatus.PARTIALLY_APPLIED
                                if deferred
                                else ExecutionRowStatus.COMMITTED
                            ),
                            attempt=1,
                            odoo_id=identifier,
                            safe_error=(
                                "Created; deferred relationship update pending"
                                if deferred
                                else ""
                            ),
                        )
                        outcomes.append(outcome)
                        if deferred:
                            deferred_by_row[row.row_id] = deferred
                        source_cache[
                            (row.dataset, _portable_key(row.source_identity))
                        ] = identifier
                        identity_cache[_identity_cache_key(row)] = identifier
                    self.journal.record_outcomes(
                        workspace_id, run.run_id, outcomes
                    )
                    recorded.update({item.row_id: item for item in outcomes})
                    report_progress(replace(run, rows=tuple(recorded.values())))
                if stop_after_unknown:
                    break

            for row in updates:
                if stop_after_unknown:
                    self._record_blocked(
                        workspace_id,
                        run.run_id,
                        (row,),
                        recorded,
                        "Not attempted after an uncertain Odoo response",
                    )
                    report_progress(replace(run, rows=tuple(recorded.values())))
                    continue
                try:
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
                    try:
                        executor.update_row(row.target_model, record_id, values)
                    except OdooWriteOutcomeUnknown as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            attempt=1,
                            safe_error=str(error),
                        )
                        stop_after_unknown = True
                    except OdooWriteRejected as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            attempt=1,
                            safe_error=str(error),
                        )
                    else:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.COMMITTED,
                            attempt=1,
                            odoo_id=record_id,
                        )
                self.journal.record_outcomes(workspace_id, run.run_id, (outcome,))
                recorded[row.row_id] = outcome
                report_progress(replace(run, rows=tuple(recorded.values())))

        if not stop_after_unknown:
            stop_after_unknown = self._apply_deferred_relationships(
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
                progress=lambda: report_progress(
                    replace(run, rows=tuple(recorded.values()))
                ),
            )
            report_progress(replace(run, rows=tuple(recorded.values())))

        remaining = tuple(
            row
            for row in write_rows
            if recorded[row.row_id].status is ExecutionRowStatus.PLANNED
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
                "Pinned Odoo loading is not available yet. No Odoo record was changed."
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
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: Mapping[tuple[str, str], int],
    ) -> tuple[FieldIntent, ...]:
        """Return optional incoming relationships whose creates are not known yet."""

        deferred = []
        for intent in row.fields:
            if not intent.defer_on_create or intent.action != "SET_VALUE":
                continue
            references = (
                intent.value if isinstance(intent.value, tuple) else (intent.value,)
            )
            for reference in references:
                if not (
                    isinstance(reference, LogicalReference)
                    and reference.origin == "incoming"
                    and reference.dataset is not None
                ):
                    continue
                source_key = (
                    reference.dataset,
                    _portable_key(reference.key),
                )
                referenced = by_source.get(source_key)
                if (
                    referenced is not None
                    and referenced.disposition == "CREATE"
                    and source_key not in source_cache
                ):
                    deferred.append(intent)
                    break
        return tuple(deferred)

    def _apply_deferred_relationships(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        deferred_by_row: Mapping[str, tuple[FieldIntent, ...]],
        recorded: dict[str, ExecutionRowAttempt],
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
        progress: Callable[[], None],
    ) -> bool:
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
                return True
            except (WorkspaceError, OdooWriteRejected) as error:
                outcome = replace(
                    attempt,
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
        return False

    def _row_values(
        self,
        row: ExecutionRow,
        metadata: Mapping[str, ExecutionDataset],
        by_source: Mapping[tuple[str, str], ExecutionRow],
        source_cache: dict[tuple[str, str], int],
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
        source_cache: dict[tuple[str, str], int],
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
            return (
                f"{intent.field}/id",
                self._relation_external_id(intent, by_source),
            )
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
        source_cache: dict[tuple[str, str], int],
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
        source_cache: dict[tuple[str, str], int],
        identity_cache: dict[str, int],
        executor: OdooWriteExecutor,
    ) -> int:
        if isinstance(value, LogicalReference) and value.origin == "incoming":
            if value.dataset is None:
                raise WorkspaceError("An incoming relationship is incomplete")
            source_key = (value.dataset, _portable_key(value.key))
            if source_key in source_cache:
                return source_cache[source_key]
            referenced = by_source.get(source_key)
            if referenced is None:
                raise WorkspaceError("A related prepared row could not be found")
            if referenced.disposition == "CREATE":
                raise WorkspaceError("A related Odoo create did not complete")
            related_id = self._find_row_id(
                referenced,
                metadata[referenced.dataset],
                identity_cache,
                executor,
            )
            source_cache[source_key] = related_id
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
        matches = executor.find_ids(model, domain)
        if len(matches) != 1:
            raise WorkspaceError(
                "The Odoo business key no longer matches exactly one record"
            )
        identity_cache[cache_key] = matches[0]
        return matches[0]

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
            if recorded[row.row_id].status is ExecutionRowStatus.PLANNED
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

    by_source = {
        (row.dataset, _portable_key(row.source_identity)): row
        for row in snapshot.rows
    }
    available: dict[tuple[str, str], int] = {}
    count = 0
    create_batch_rows = validated_create_batch_rows(create_batch_rows)
    for dataset in sorted(snapshot.datasets, key=lambda item: item.sequence):
        creates = tuple(
            row
            for row in snapshot.rows
            if row.dataset == dataset.dataset and row.disposition == "CREATE"
        )
        for start in range(0, len(creates), create_batch_rows):
            batch = creates[start : start + create_batch_rows]
            count += sum(
                bool(
                    ExecutionService._deferred_create_intents(
                        row,
                        by_source,
                        available,
                    )
                )
                for row in batch
            )
            for row in batch:
                available[
                    (row.dataset, _portable_key(row.source_identity))
                ] = 1
    return count


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
        if referenced is None or referenced.target_model != intent.related_model:
            return (
                f"{dataset.dataset}.{intent.field} has no reviewed imported record"
            )
        if related_dataset.sequence >= dataset.sequence:
            if not intent.defer_on_create:
                return (
                    f"{dataset.dataset}.{intent.field} is required during create "
                    "and cannot participate in a dependency cycle"
                )
            if (
                referenced.disposition != "CREATE"
                or not referenced.proposed_external_id
            ):
                return (
                    f"{dataset.dataset}.{intent.field} has no deferred create record"
                )
            return ""
        if require_created and (
            referenced.disposition != "CREATE"
            or not referenced.proposed_external_id
        ):
            return (
                f"{dataset.dataset}.{intent.field} has no earlier imported record"
            )
        if not require_created and referenced.disposition not in {
            "CREATE",
            "UPDATE",
            "UNCHANGED",
        }:
            return (
                f"{dataset.dataset}.{intent.field} has no usable earlier record"
            )
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
