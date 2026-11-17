"""Classify exact recovery evidence without authorizing or recording writes.

The durable journal remains authoritative. These pure decisions retain its
receipts; application use cases separately authorize and atomically persist them.
"""

from __future__ import annotations

import re
from dataclasses import replace

from impodo.domain.execution.load_control import LoadControlState, RejectionPolicy
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
)
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.reconciliation import ReconciliationRowStatus, ReconciliationRun
from impodo.domain.shared.models import BusinessReference, LogicalReference
from impodo.domain.workspace.errors import WorkspaceError

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def targeted_recovery_row_ids(
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
    dataset_parents = {item.dataset: item.dependencies for item in snapshot.datasets}
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
                values = (
                    intent.value if isinstance(intent.value, tuple) else (intent.value,)
                )
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


def projected_receipt_requirements(
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
    return {row_id: frozenset(items) for row_id, items in requirements.items()}


def classify_recovery(
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
    allowed_scopes = {"FULL", "RECOVERY_TARGETED"} if targeted else {"FULL"}
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
        or not _SHA256.fullmatch(recovery.verification_credential_binding_hash)
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
    projected_requirements = projected_receipt_requirements(snapshot)
    required_ids = (
        targeted_recovery_row_ids(snapshot, run) if targeted else frozenset(rows)
    )
    if (
        set(rows) != set(attempts)
        or not required_ids.issubset(outcomes)
        or not set(outcomes).issubset(rows)
        or (recovery.readback_scope == "FULL" and set(outcomes) != set(rows))
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
            projected_requirements.get(row_id, frozenset()).difference(projected_keys)
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
                safe_error = (
                    "Read-back proved that the interrupted create was not applied"
                )
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
            and outcome.status
            in {
                ReconciliationRowStatus.NOT_WRITTEN,
                ReconciliationRowStatus.VERIFIED,
                ReconciliationRowStatus.DIFFERENT,
            }
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
