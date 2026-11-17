"""Order scheduled writes while focused collaborators own values and receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from impodo.application.shared.local_operations import (
    LocalOperation,
    direct_local_operation,
)
from impodo.domain.execution.batch_policy import validated_create_batch_rows
from impodo.domain.execution.dependency_scheduler import dependency_component_pages
from impodo.domain.execution.identity import portable_key
from impodo.domain.execution.load_control import RejectionPolicy, can_set_aside
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_write import (
    OdooWriteError,
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.recovery import projected_receipt_requirements
from impodo.domain.execution_snapshot import (
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
)
from impodo.domain.mapping.source_record_identity import SOURCE_EXTERNAL_IDS_FIELD
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
)

from .identity_resolution import assert_create_rows_still_absent, identity_cache_key
from .journal import (
    ExecutionJournalRepository,
    JournalTransitions,
    require_dependency_receipts,
    write_diagnostic,
)
from .load_observation import observe_load_local_phase, observe_load_transport
from .load_transport import LoadControlRepository, admit_write, create_transport_results
from .progress import ExecutionProgressPublisher
from .relationship_completion import RelationshipCompletion, deferred_create_intents
from .write_values import find_row_id, row_values


class LoadWriter:
    """Keep durable scheduling and stop decisions in one explicit coordinator."""

    def __init__(
        self,
        journal: ExecutionJournalRepository,
        load_controls: LoadControlRepository | None,
        *,
        local_operation: LocalOperation | None = None,
    ) -> None:
        self.journal = journal
        self.load_controls = load_controls
        self._run_local = local_operation or direct_local_operation

    def run(
        self,
        workspace_state: WorkspaceState,
        snapshot: ExecutionSnapshot,
        run: ExecutionRun,
        executor: OdooWriteExecutor,
        actor: Actor,
        *,
        identity_cache: dict[str, int],
        progress: ExecutionProgressPublisher,
        protected_values: Mapping[str, Any] | None = None,
        create_with_external_ids: bool = False,
    ) -> ExecutionRun:
        """Consume unfinished rows from one durable schedule."""

        create_with_external_ids = create_with_external_ids or any(
            SOURCE_EXTERNAL_IDS_FIELD in item.identity_fields
            for item in snapshot.datasets
        )
        workspace_id = workspace_state.workspace_id
        write_rows = tuple(
            row for row in snapshot.rows if row.disposition in {"CREATE", "UPDATE"}
        )
        metadata = {item.dataset: item for item in snapshot.datasets}
        by_source = {
            (row.dataset, portable_key(row.source_identity)): row
            for row in snapshot.rows
        }
        create_batch_rows = validated_create_batch_rows(run.batch_rows or 0)
        progress.publish(force=True)
        recorded: dict[str, ExecutionRowAttempt] = {
            item.row_id: item for item in run.rows
        }
        transitions = JournalTransitions(
            self.journal, recorded, progress, protected_values=protected_values,
            local_operation=self._run_local,
        )
        completion = RelationshipCompletion(transitions)
        accepted_models = {
            item.target_model
            for item in run.rows
            if item.status
            in {ExecutionRowStatus.COMMITTED, ExecutionRowStatus.PARTIALLY_APPLIED}
        }
        continuation_policy = bool(
            run.load_control and run.load_control.policy is RejectionPolicy.CONTINUE
        )
        if (
            continuation_policy
            and run.load_control is not None
            and not run.load_control.windows
        ):
            accepted_models.clear()

        source_cache: dict[tuple[str, str, str, str], int] = {
            (
                row.dataset,
                portable_key(row.source_identity),
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
                        portable_key(row.source_identity),
                        receipt.target_model,
                        receipt.projection_field,
                    )
                ] = receipt.odoo_id
        for row in write_rows:
            attempt = recorded[row.row_id]
            if attempt.odoo_id is not None:
                identity_cache[identity_cache_key(row)] = attempt.odoo_id
        deferred_by_row: dict[str, tuple[FieldIntent, ...]] = {
            row.row_id: deferred
            for row in write_rows
            if recorded[row.row_id].status is ExecutionRowStatus.PARTIALLY_APPLIED
            and (deferred := deferred_create_intents(row))
        }
        projected_requirements = projected_receipt_requirements(snapshot)
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
                transitions.record_blocked(
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
                progress.publish(changed_rows=len(dataset_rows))
                continue
            try:
                completion.ensure_projected_receipts(
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
                transitions.record_blocked(
                    workspace_id,
                    run.run_id,
                    dataset_rows,
                    recorded,
                    str(error),
                )
                progress.publish(changed_rows=len(dataset_rows))
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
                and transitions.has_committed_rows
            ):
                # A prior wave can cause Odoo to generate records that were
                # absent at the pre-journal check. Never import those children
                # as fresh rows from an already approved snapshot.
                try:
                    assert_create_rows_still_absent(creates, metadata, executor)
                except WorkspaceError as error:
                    transitions.record_blocked(
                        workspace_id,
                        run.run_id,
                        dataset_rows,
                        recorded,
                        str(error),
                    )
                    progress.publish(changed_rows=len(dataset_rows))
                    stop_after_rejection = True
                    continue
            start = 0
            while start < len(creates):
                group_size = (
                    min(create_batch_rows, 10)
                    if continuation_policy
                    and dataset.target_model not in accepted_models
                    else create_batch_rows
                )
                batch = creates[start : start + group_size]
                start += len(batch)
                prepared_rows: list[
                    tuple[ExecutionRow, dict[str, Any], tuple[FieldIntent, ...]]
                ] = []
                for row in batch:
                    try:
                        require_dependency_receipts(
                            row,
                            row_by_id,
                            recorded,
                        )
                        deferred = deferred_create_intents(row)
                        values = row_values(
                            row,
                            metadata,
                            by_source,
                            source_cache,
                            identity_cache,
                            executor,
                            protected_values=protected_values,
                            import_relations=(
                                create_with_external_ids
                                or SOURCE_EXTERNAL_IDS_FIELD
                                in metadata[row.dataset].identity_fields
                                or workspace_state.odoo_connection_mode
                                is OdooConnectionMode.REMOTE
                            ),
                            skip_fields=frozenset(intent.field for intent in deferred),
                        )
                    except (WorkspaceError, OdooWriteRejected) as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.BLOCKED,
                            safe_error=str(error),
                        )
                        transitions.record_outcomes(
                            workspace_id, run.run_id, (outcome,)
                        )
                        recorded[row.row_id] = outcome
                        progress.publish(changed_rows=1)
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
                        or workspace_state.odoo_connection_mode
                        is OdooConnectionMode.REMOTE
                        or any(
                            SOURCE_EXTERNAL_IDS_FIELD
                            in metadata[item[0].dataset].identity_fields
                            for item in prepared_group
                        )
                    )
                    continuation = bool(
                        run.load_control
                        and run.load_control.policy is RejectionPolicy.CONTINUE
                    )

                    def begin(
                        group_rows: tuple[ExecutionRow, ...],
                        *,
                        component: int = page.component_sequence,
                        page_sequence: int = page.page_sequence,
                    ) -> None:
                        nonlocal next_transport_batch
                        transitions.start_transport_batch(
                            workspace_id,
                            run.run_id,
                            group_rows,
                            recorded,
                            phase="CREATE",
                            component=component,
                            page=page_sequence,
                            batch=next_transport_batch,
                        )
                        next_transport_batch += 1

                    def rolled_back(
                        group_rows: tuple[ExecutionRow, ...], error: OdooWriteRejected
                    ) -> None:
                        if error.operator_detail is not None:
                            error.operator_detail = error.operator_detail.redact(
                                tuple(str(v) for v in (protected_values or {}).values())
                            )
                        if self.load_controls is None:
                            raise WorkspaceError(
                                "Load isolation has no durable journal",
                                load_failure_code="LOAD_LOAD_ISOLATION_HAS_NO_DURABLE_JOURNAL",
                            )
                        transitions.record_rejected_batch(
                            self.load_controls,
                            workspace_id,
                            run.run_id,
                            tuple(recorded[row.row_id] for row in group_rows),
                            error,
                        )

                    for result in create_transport_results(
                        prepared_group,
                        model=dataset.target_model,
                        executor=executor,
                        import_create=import_create,
                        continuation=continuation,
                        begin=begin,
                        rolled_back=rolled_back,
                    ):
                        error = result.error
                        if error is not None:
                            unknown = isinstance(error, OdooWriteOutcomeUnknown)
                            outcomes = tuple(
                                replace(
                                    recorded[row.row_id],
                                    status=ExecutionRowStatus.OUTCOME_UNKNOWN
                                    if unknown
                                    else ExecutionRowStatus.FAILED,
                                    safe_error=str(error),
                                    diagnostic=write_diagnostic(
                                        error,
                                        row,
                                        len(result.prepared),
                                        method="load" if import_create else "create",
                                    ),
                                )
                                for row, _values, _deferred in result.prepared
                            )
                            transitions.record_outcomes(
                                workspace_id,
                                run.run_id,
                                outcomes,
                                error=error,
                                protected_values=protected_values,
                            )
                            recorded.update({item.row_id: item for item in outcomes})
                            progress.publish(changed_rows=len(outcomes), force=True)
                            if unknown:
                                stop_after_unknown = True
                            elif not (
                                continuation
                                and isinstance(error, OdooWriteRejected)
                                and can_set_aside(error)
                            ):
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
                                    portable_key(row.source_identity),
                                    row.target_model,
                                    "",
                                )
                            ] = identifier
                            identity_cache[identity_cache_key(row)] = identifier
                        transitions.record_outcomes(workspace_id, run.run_id, outcomes)
                        recorded.update({item.row_id: item for item in outcomes})
                        progress.publish(changed_rows=len(outcomes))
                if stop_after_unknown or stop_after_rejection:
                    break

            for row in updates:
                operator_error: OdooWriteError | None = None
                if stop_after_unknown or stop_after_rejection:
                    transitions.record_blocked(
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
                    progress.publish(changed_rows=1)
                    continue
                try:
                    require_dependency_receipts(
                        row,
                        row_by_id,
                        recorded,
                    )
                    record_id = find_row_id(
                        row,
                        metadata[row.dataset],
                        identity_cache,
                        executor,
                    )
                    values = row_values(
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
                    admit_write(executor)
                    transitions.start_transport_batch(
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
                        with observe_load_transport(
                            row.target_model, "write", tuple(values), 1
                        ):
                            executor.update_row(row.target_model, record_id, values)
                    except OdooWriteOutcomeUnknown as error:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                            safe_error=str(error),
                            diagnostic=write_diagnostic(error, row),
                        )
                        stop_after_unknown = True
                    except OdooWriteRejected as error:
                        operator_error = error
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.FAILED,
                            safe_error=str(error),
                            diagnostic=write_diagnostic(error, row),
                        )
                        stop_after_rejection = not (
                            run.load_control
                            and run.load_control.policy is RejectionPolicy.CONTINUE
                            and can_set_aside(error)
                        )
                    else:
                        outcome = replace(
                            recorded[row.row_id],
                            status=ExecutionRowStatus.COMMITTED,
                            odoo_id=record_id,
                        )
                transitions.record_outcomes(
                    workspace_id,
                    run.run_id,
                    (outcome,),
                    error=operator_error,
                    protected_values=protected_values,
                )
                recorded[row.row_id] = outcome
                progress.publish(
                    changed_rows=1,
                    force=stop_after_unknown or stop_after_rejection,
                )

        if not stop_after_unknown and not stop_after_rejection:
            # Preserve the visible boundary between first-pass writes and
            # relationship completion even when the final create batch is
            # smaller than the configured transport batch.
            progress.publish(force=True)
            completion_stopped, next_transport_batch = (
                completion.apply_deferred_relationships(
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
                    progress=lambda: progress.publish(changed_rows=1),
                    continuation=bool(
                        run.load_control
                        and run.load_control.policy is RejectionPolicy.CONTINUE
                    ),
                )
            )
            stop_after_unknown = completion_stopped
            progress.publish(force=True)

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
            def settle_remaining() -> None:
                if self.load_controls is not None:
                    control = self.load_controls.get_load_control(workspace_id, run.run_id)
                    if control is not None:
                        control.require_running()
                transitions.record_blocked(
                    workspace_id,
                    run.run_id,
                    remaining,
                    recorded,
                    "The load ended before this record was sent to Odoo",
                )

            with observe_load_local_phase("execution_checks"):
                self._run_local(settle_remaining)
            progress.publish(changed_rows=len(remaining), force=True)
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
        with observe_load_local_phase("execution_checks"):
            completed_run = self._run_local(
                lambda: self.journal.finish_run(
                    workspace_id,
                    run.run_id,
                    final_status,
                    actor=actor,
                )
            )
        progress.finish(completed_run)
        return completed_run
