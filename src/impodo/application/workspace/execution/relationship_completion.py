"""Complete only frozen generated and deferred relationships after creates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from impodo.domain.execution.identity import portable_key
from impodo.domain.execution.load_control import can_set_aside
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ProjectedOdooReceipt,
)
from impodo.domain.execution.odoo_write import (
    MAX_PROJECTED_RECEIPT_IDS,
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution_snapshot import ExecutionDataset, ExecutionRow, FieldIntent
from impodo.domain.workspace.errors import WorkspaceError

from .journal import JournalTransitions, write_diagnostic
from .load_observation import observe_load_transport
from .load_transport import DependentCompletions, admit_write
from .write_values import find_row_id, relation_value


def deferred_create_intents(
    row: ExecutionRow,
) -> tuple[FieldIntent, ...]:
    """Return the exact completion fields frozen by row scheduling."""

    return tuple(
        intent
        for intent in row.fields
        if intent.defer_on_create and intent.action == "SET_VALUE"
    )


class RelationshipCompletion:
    """Apply exact completion fields through the owning journal boundary."""

    def __init__(self, transitions: JournalTransitions) -> None:
        self.transitions = transitions

    def ensure_projected_receipts(
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
        *,
        continuation: bool,
    ) -> None:
        """Read and journal exact generated records before their consumers."""

        grouped: dict[
            tuple[str, str, str],
            dict[str, tuple[ExecutionRow, int]],
        ] = {}
        blocked_consumers: dict[str, ExecutionRow] = {}
        for consumer in consumer_rows:
            for intent in consumer.fields:
                projection_field = intent.incoming_projection_field
                if not projection_field:
                    continue
                for dependency_row_id in intent.dependency_row_ids:
                    dependency = row_by_id[dependency_row_id]
                    cache_key = (
                        dependency.dataset,
                        portable_key(dependency.source_identity),
                        intent.related_model,
                        projection_field,
                    )
                    if cache_key in source_cache:
                        continue
                    if dependency.disposition == "CREATE":
                        attempt = recorded.get(dependency_row_id)
                        if continuation and attempt is not None and attempt.status in {
                            ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED,
                        }:
                            blocked_consumers[consumer.row_id] = consumer
                            continue
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
                        source_id = find_row_id(
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

        if blocked_consumers:
            self.transitions.record_blocked(
                workspace_id, run_id, tuple(blocked_consumers.values()), recorded,
                "A generated relationship source was not created",
            )

        observed: list[tuple[ExecutionRow, str, str, int]] = []
        for (
            source_model,
            projection_field,
            target_model,
        ), dependencies in sorted(grouped.items()):
            ordered = tuple(dependencies[row_id] for row_id in sorted(dependencies))
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
                    projected_receipts=tuple(receipts[key] for key in sorted(receipts)),
                )
            )
        if outcomes:
            self.transitions.record_outcomes(workspace_id, run_id, outcomes)
            recorded.update({item.row_id: item for item in outcomes})

        for row, projection_field, target_model, projected_id in observed:
            source_cache[
                (
                    row.dataset,
                    portable_key(row.source_identity),
                    target_model,
                    projection_field,
                )
            ] = projected_id

    def apply_deferred_relationships(
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

        dependencies = (
            DependentCompletions(
                rows,
                tuple(
                    row.row_id
                    for row in rows
                    if recorded[row.row_id].status
                    in {ExecutionRowStatus.FAILED, ExecutionRowStatus.BLOCKED}
                    or (
                        recorded[row.row_id].status
                        is ExecutionRowStatus.PARTIALLY_APPLIED
                        and recorded[row.row_id].diagnostic is not None
                    )
                ),
            )
            if continuation
            else None
        )
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
                outcome = replace(
                    attempt,
                    safe_error="Created; relationship completion is waiting because a required record was rejected",
                )
                self.transitions.record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                progress()
                continue
            record_id = attempt.odoo_id
            try:
                values = {
                    intent.field: relation_value(
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
                self.transitions.record_outcomes(workspace_id, run_id, (outcome,))
                recorded[row.row_id] = outcome
                if dependencies is not None:
                    dependencies.reject(row.row_id)
                progress()
                continue
            admit_write(executor)
            self.transitions.start_transport_batch(
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
            operator_error = None
            try:
                with observe_load_transport(
                    row.target_model, "write", tuple(values), 1
                ):
                    executor.update_row(row.target_model, record_id, values)
            except OdooWriteOutcomeUnknown as error:
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.OUTCOME_UNKNOWN,
                    safe_error=str(error),
                    diagnostic=write_diagnostic(error, row, method="write"),
                )
                self.transitions.record_outcomes(
                    workspace_id, run_id, (outcome,), error=operator_error
                )
                recorded[row.row_id] = outcome
                progress()
                return True, next_transport_batch
            except OdooWriteRejected as error:
                operator_error = error
                outcome = replace(
                    attempt,
                    status=ExecutionRowStatus.PARTIALLY_APPLIED,
                    diagnostic=write_diagnostic(error, row, method="write"),
                    safe_error=(
                        "Record was created, but its deferred relationship "
                        f"update failed: {error}"
                    ),
                )
                if not continuation or not can_set_aside(error):
                    self.transitions.record_outcomes(
                        workspace_id, run_id, (outcome,), error=operator_error
                    )
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
            self.transitions.record_outcomes(
                workspace_id, run_id, (outcome,), error=operator_error
            )
            recorded[row.row_id] = outcome
            progress()
        return False, next_transport_batch
