"""Classify supplied execution shape without workspace or credential lookups."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    dependency_ordered_execution_datasets,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.shared.models import BusinessReference, LogicalReference
from impodo.domain.target_numeric_precision import (
    TargetNumericPrecisionLosses,
    TargetNumericPrecisionSummary,
)
from impodo.domain.workspace.workbench import OdooConnectionMode

from .identity import portable_key

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class ExecutionSnapshotIssueCode(StrEnum):
    """Stable shape classifications; presentation owns their explanations."""

    TARGET_NOT_CONFIGURED = "TARGET_NOT_CONFIGURED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    SCHEDULE_CHANGED = "SCHEDULE_CHANGED"
    HARD_DEPENDENCY_CYCLE = "HARD_DEPENDENCY_CYCLE"
    BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
    INVALID_DATASET_ORDER = "INVALID_DATASET_ORDER"
    STALE_DATASET_ORDER = "STALE_DATASET_ORDER"
    NO_WRITE_ROWS = "NO_WRITE_ROWS"
    NUMERIC_PRECISION_LOSS = "NUMERIC_PRECISION_LOSS"
    NON_UNIQUE_RELATIONSHIP = "NON_UNIQUE_RELATIONSHIP"
    INCOMPLETE_INCOMING_REFERENCE = "INCOMPLETE_INCOMING_REFERENCE"
    UNREVIEWED_DEPENDENCY = "UNREVIEWED_DEPENDENCY"
    MISSING_INCOMING_RECORD = "MISSING_INCOMING_RECORD"
    MISSING_ROW_DEPENDENCY = "MISSING_ROW_DEPENDENCY"
    UNUSABLE_INCOMING_RECORD = "UNUSABLE_INCOMING_RECORD"
    MISSING_INCOMING_CREATE = "MISSING_INCOMING_CREATE"
    MISSING_DATASET = "MISSING_DATASET"
    REMOTE_UPDATE_OPERATION = "REMOTE_UPDATE_OPERATION"
    RELATIONSHIP_MODEL_MISMATCH = "RELATIONSHIP_MODEL_MISMATCH"
    UNREVIEWED_BUSINESS_KEY = "UNREVIEWED_BUSINESS_KEY"
    MISSING_EXTERNAL_ID = "MISSING_EXTERNAL_ID"
    REMOTE_MANY2MANY_CREATE_OPERATION = "REMOTE_MANY2MANY_CREATE_OPERATION"
    UNSUPPORTED_RELATIONSHIP_VALUE = "UNSUPPORTED_RELATIONSHIP_VALUE"
    REMOTE_MANY2ONE_CREATE_OPERATION = "REMOTE_MANY2ONE_CREATE_OPERATION"
    UNSUPPORTED_RELATIONSHIP_OPERATION = "UNSUPPORTED_RELATIONSHIP_OPERATION"


@dataclass(frozen=True, slots=True)
class ExecutionSnapshotIssue:
    """One ordered rejection with the exact facts needed for presentation."""

    code: ExecutionSnapshotIssueCode
    dataset: str = ""
    field: str = ""
    relation_operation: str = ""
    precision: TargetNumericPrecisionSummary | None = None


def _schedule_issue(snapshot: ExecutionSnapshot) -> ExecutionSnapshotIssue | None:
    plan = snapshot.relationship_plan
    scheduled_rows = tuple(
        sorted(
            (row for row in snapshot.rows if row.schedule_ordinal >= 0),
            key=lambda row: row.schedule_ordinal,
        )
    )
    component_row_ids = tuple(
        row_id for component in plan.components for row_id in component.row_ids
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
        if row.disposition in {"CREATE", "UPDATE"} and row.row_id not in blocked_row_ids
    }
    completion_fields = {(item.row_id, item.field) for item in plan.completions}
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
            row.schedule_component != schedule_component_by_row.get(row.row_id, -1)
            for row in scheduled_rows
        )
        or completion_fields != deferred_fields
    ):
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.SCHEDULE_CHANGED)
    if plan.blockers:
        return ExecutionSnapshotIssue(
            ExecutionSnapshotIssueCode.HARD_DEPENDENCY_CYCLE
            if plan.blockers[0].code == "HARD_DEPENDENCY_CYCLE"
            else ExecutionSnapshotIssueCode.BLOCKED_DEPENDENCY
        )
    return None


def _dataset_order_issue(
    snapshot: ExecutionSnapshot,
) -> ExecutionSnapshotIssue | None:
    sequenced = tuple(sorted(snapshot.datasets, key=lambda item: item.sequence))
    try:
        dependency_ordered = dependency_ordered_execution_datasets(sequenced)
    except ValueError:
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.INVALID_DATASET_ORDER)
    if tuple(item.dataset for item in sequenced) != tuple(
        item.dataset for item in dependency_ordered
    ):
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.STALE_DATASET_ORDER)
    return None


def _precision_issue(
    write_rows: Sequence[ExecutionRow],
    datasets: Sequence[ExecutionDataset],
) -> ExecutionSnapshotIssue | None:
    losses = TargetNumericPrecisionLosses()
    digits_by_dataset = {item.dataset: dict(item.field_digits) for item in datasets}
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
            losses.add(row.target_model, intent.field, digits, intent.value)
    summary = losses.first_summary()
    return (
        ExecutionSnapshotIssue(
            ExecutionSnapshotIssueCode.NUMERIC_PRECISION_LOSS, precision=summary
        )
        if summary is not None
        else None
    )


def _field_issue(
    code: ExecutionSnapshotIssueCode,
    dataset: ExecutionDataset,
    intent: FieldIntent,
) -> ExecutionSnapshotIssue:
    return ExecutionSnapshotIssue(code, dataset=dataset.dataset, field=intent.field)


def _incoming_reference_issue(
    value: LogicalReference,
    intent: FieldIntent,
    dataset: ExecutionDataset,
    datasets: Mapping[str, ExecutionDataset],
    rows_by_source: Mapping[tuple[str, str], ExecutionRow],
    *,
    require_created: bool = True,
) -> ExecutionSnapshotIssue | None:
    if value.origin != "incoming":
        return _field_issue(
            ExecutionSnapshotIssueCode.NON_UNIQUE_RELATIONSHIP, dataset, intent
        )
    if value.dataset is None:
        return _field_issue(
            ExecutionSnapshotIssueCode.INCOMPLETE_INCOMING_REFERENCE, dataset, intent
        )
    related_dataset = datasets.get(value.dataset)
    if related_dataset is None or value.dataset not in dataset.dependencies:
        return _field_issue(
            ExecutionSnapshotIssueCode.UNREVIEWED_DEPENDENCY, dataset, intent
        )
    referenced = rows_by_source.get((value.dataset, portable_key(value.key)))
    if (
        referenced is None
        or (
            referenced.target_model != intent.related_model
            and not intent.incoming_projection_field
        )
        or (
            referenced.target_model == intent.related_model
            and intent.incoming_projection_field
        )
    ):
        return _field_issue(
            ExecutionSnapshotIssueCode.MISSING_INCOMING_RECORD, dataset, intent
        )
    if referenced.row_id not in intent.dependency_row_ids:
        return _field_issue(
            ExecutionSnapshotIssueCode.MISSING_ROW_DEPENDENCY, dataset, intent
        )
    if referenced.disposition not in {"CREATE", "UPDATE", "UNCHANGED"}:
        return _field_issue(
            ExecutionSnapshotIssueCode.UNUSABLE_INCOMING_RECORD, dataset, intent
        )
    if (
        require_created
        and referenced.disposition == "CREATE"
        and not referenced.proposed_external_id
        and not intent.incoming_projection_field
    ):
        return _field_issue(
            ExecutionSnapshotIssueCode.MISSING_INCOMING_CREATE, dataset, intent
        )
    return None


def _remote_update_issue(
    row: ExecutionRow,
    dataset: ExecutionDataset,
    datasets: Mapping[str, ExecutionDataset],
    rows_by_source: Mapping[tuple[str, str], ExecutionRow],
) -> ExecutionSnapshotIssue | None:
    for intent in row.fields:
        if (
            intent.action == "OMIT"
            or intent.kind == "scalar"
            or intent.action == "SET_NULL"
        ):
            continue
        if intent.action != "SET_VALUE" or intent.relation_operation != "replace":
            return _field_issue(
                ExecutionSnapshotIssueCode.REMOTE_UPDATE_OPERATION, dataset, intent
            )
        values = intent.value if isinstance(intent.value, tuple) else (intent.value,)
        for value in values:
            if isinstance(value, LogicalReference):
                issue = _incoming_reference_issue(
                    value,
                    intent,
                    dataset,
                    datasets,
                    rows_by_source,
                    require_created=False,
                )
                if issue is not None:
                    return issue
            elif isinstance(value, BusinessReference):
                if value.model != intent.related_model:
                    return _field_issue(
                        ExecutionSnapshotIssueCode.RELATIONSHIP_MODEL_MISMATCH,
                        dataset,
                        intent,
                    )
            else:
                return _field_issue(
                    ExecutionSnapshotIssueCode.UNREVIEWED_BUSINESS_KEY, dataset, intent
                )
    return None


def _remote_create_issue(
    row: ExecutionRow,
    dataset: ExecutionDataset,
    datasets: Mapping[str, ExecutionDataset],
    rows_by_source: Mapping[tuple[str, str], ExecutionRow],
) -> ExecutionSnapshotIssue | None:
    if not row.proposed_external_id:
        return ExecutionSnapshotIssue(
            ExecutionSnapshotIssueCode.MISSING_EXTERNAL_ID, dataset=row.dataset
        )
    for intent in row.fields:
        if intent.action in {"OMIT", "SET_NULL", "EXPECT_PROTECTED"}:
            continue
        if intent.kind == "scalar":
            continue
        if intent.action == "SET_PROTECTED" and intent.relation_operation == "replace":
            continue
        value = intent.value
        if isinstance(value, tuple):
            if intent.action != "SET_VALUE" or intent.relation_operation not in {
                "replace",
                "add",
            }:
                return _field_issue(
                    ExecutionSnapshotIssueCode.REMOTE_MANY2MANY_CREATE_OPERATION,
                    dataset,
                    intent,
                )
            for item in value:
                if isinstance(item, LogicalReference):
                    issue = _incoming_reference_issue(
                        item,
                        intent,
                        dataset,
                        datasets,
                        rows_by_source,
                        require_created=False,
                    )
                    if issue is not None:
                        return issue
                elif isinstance(item, BusinessReference):
                    if item.model != intent.related_model:
                        return _field_issue(
                            ExecutionSnapshotIssueCode.RELATIONSHIP_MODEL_MISMATCH,
                            dataset,
                            intent,
                        )
                else:
                    return _field_issue(
                        ExecutionSnapshotIssueCode.UNSUPPORTED_RELATIONSHIP_VALUE,
                        dataset,
                        intent,
                    )
            continue
        if intent.action != "SET_VALUE" or intent.relation_operation != "replace":
            return _field_issue(
                ExecutionSnapshotIssueCode.REMOTE_MANY2ONE_CREATE_OPERATION,
                dataset,
                intent,
            )
        if isinstance(value, LogicalReference):
            issue = _incoming_reference_issue(
                value, intent, dataset, datasets, rows_by_source
            )
            if issue is not None:
                return issue
        elif isinstance(value, BusinessReference):
            if value.model != intent.related_model:
                return _field_issue(
                    ExecutionSnapshotIssueCode.RELATIONSHIP_MODEL_MISMATCH,
                    dataset,
                    intent,
                )
        else:
            return _field_issue(
                ExecutionSnapshotIssueCode.UNREVIEWED_BUSINESS_KEY, dataset, intent
            )
    return None


def _relationship_value_issue(row: ExecutionRow) -> ExecutionSnapshotIssue | None:
    for intent in row.fields:
        if intent.kind == "scalar" or intent.action != "SET_VALUE":
            continue
        if isinstance(intent.value, tuple):
            if intent.relation_operation not in {"replace", "add", "remove"}:
                return ExecutionSnapshotIssue(
                    ExecutionSnapshotIssueCode.UNSUPPORTED_RELATIONSHIP_OPERATION,
                    dataset=row.dataset,
                    field=intent.field,
                    relation_operation=intent.relation_operation,
                )
            if not all(
                isinstance(item, BusinessReference | LogicalReference)
                for item in intent.value
            ):
                return ExecutionSnapshotIssue(
                    ExecutionSnapshotIssueCode.UNSUPPORTED_RELATIONSHIP_VALUE,
                    dataset=row.dataset,
                    field=intent.field,
                )
        elif not isinstance(intent.value, BusinessReference | LogicalReference):
            return ExecutionSnapshotIssue(
                ExecutionSnapshotIssueCode.UNREVIEWED_BUSINESS_KEY,
                dataset=row.dataset,
                field=intent.field,
            )
    return None


def execution_snapshot_issue(
    snapshot: ExecutionSnapshot,
    *,
    connection_mode: OdooConnectionMode | None,
    operation: OdooOperation = OdooOperation.WRITE,
) -> ExecutionSnapshotIssue | None:
    """Return the first shape issue in the established admission order."""
    if connection_mode not in {OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE}:
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.TARGET_NOT_CONFIGURED)
    if not assess_odoo_operation(snapshot.target_odoo_version, operation).allowed:
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.UNSUPPORTED_VERSION)
    issue = _schedule_issue(snapshot) or _dataset_order_issue(snapshot)
    if issue is not None:
        return issue
    write_rows = tuple(row for row in snapshot.rows if row.fields)
    if not write_rows:
        return ExecutionSnapshotIssue(ExecutionSnapshotIssueCode.NO_WRITE_ROWS)
    issue = _precision_issue(write_rows, snapshot.datasets)
    if issue is not None:
        return issue
    datasets = {item.dataset: item for item in snapshot.datasets}
    rows_by_source = {
        (row.dataset, portable_key(row.source_identity)): row for row in snapshot.rows
    }
    for row in write_rows:
        dataset = datasets.get(row.dataset)
        if dataset is None:
            return ExecutionSnapshotIssue(
                ExecutionSnapshotIssueCode.MISSING_DATASET, dataset=row.dataset
            )
        if connection_mode is OdooConnectionMode.REMOTE:
            if row.disposition == "UPDATE":
                issue = _remote_update_issue(row, dataset, datasets, rows_by_source)
            elif row.disposition == "CREATE":
                issue = _remote_create_issue(row, dataset, datasets, rows_by_source)
            else:
                issue = None
            if issue is not None:
                return issue
        issue = _relationship_value_issue(row)
        if issue is not None:
            return issue
    return None
