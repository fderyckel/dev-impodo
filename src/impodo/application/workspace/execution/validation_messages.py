"""Map execution-shape issues to the accepted operator explanations."""

from __future__ import annotations

from impodo.domain.execution.validation import (
    ExecutionSnapshotIssue,
    ExecutionSnapshotIssueCode,
)

NO_WRITE_ROWS_MESSAGE = "This preview has no rows to create or update"

_SNAPSHOT_MESSAGES: dict[ExecutionSnapshotIssueCode, str] = {
    ExecutionSnapshotIssueCode.TARGET_NOT_CONFIGURED: "Configure the exact Odoo load target first",
    ExecutionSnapshotIssueCode.UNSUPPORTED_VERSION: (
        "This Odoo version is not enabled for the schema-bound load path"
    ),
    ExecutionSnapshotIssueCode.SCHEDULE_CHANGED: (
        "The reviewed relationship schedule changed. Compare with Odoo "
        "again before loading."
    ),
    ExecutionSnapshotIssueCode.HARD_DEPENDENCY_CYCLE: (
        "Required create-time relationships form a row cycle. Revise "
        "those relationships before loading."
    ),
    ExecutionSnapshotIssueCode.BLOCKED_DEPENDENCY: (
        "A reviewed relationship dependency cannot be scheduled safely. "
        "Resolve the blocked imported relationship before loading."
    ),
    ExecutionSnapshotIssueCode.INVALID_DATASET_ORDER: (
        "The reviewed dataset dependency order is invalid. Compare with "
        "Odoo again before loading."
    ),
    ExecutionSnapshotIssueCode.STALE_DATASET_ORDER: (
        "The reviewed dataset order is stale. Compare with Odoo again so "
        "dependencies are loaded first."
    ),
    ExecutionSnapshotIssueCode.NO_WRITE_ROWS: NO_WRITE_ROWS_MESSAGE,
    ExecutionSnapshotIssueCode.NON_UNIQUE_RELATIONSHIP: (
        "{dataset}.{field} has no unique reviewed Odoo relationship match"
    ),
    ExecutionSnapshotIssueCode.INCOMPLETE_INCOMING_REFERENCE: (
        "{dataset}.{field} has an incomplete imported relationship reference"
    ),
    ExecutionSnapshotIssueCode.UNREVIEWED_DEPENDENCY: (
        "{dataset}.{field} must reference a reviewed dependency dataset"
    ),
    ExecutionSnapshotIssueCode.MISSING_INCOMING_RECORD: (
        "{dataset}.{field} has no reviewed imported record"
    ),
    ExecutionSnapshotIssueCode.MISSING_ROW_DEPENDENCY: (
        "{dataset}.{field} is missing its reviewed row dependency"
    ),
    ExecutionSnapshotIssueCode.UNUSABLE_INCOMING_RECORD: (
        "{dataset}.{field} has no usable imported record"
    ),
    ExecutionSnapshotIssueCode.MISSING_INCOMING_CREATE: (
        "{dataset}.{field} has no imported create record"
    ),
    ExecutionSnapshotIssueCode.MISSING_DATASET: (
        "Dataset {dataset} is missing from the reviewed load preview"
    ),
    ExecutionSnapshotIssueCode.REMOTE_UPDATE_OPERATION: (
        "The remote-update slice supports exact relationship "
        "replacement; {dataset}.{field} is outside it"
    ),
    ExecutionSnapshotIssueCode.RELATIONSHIP_MODEL_MISMATCH: (
        "{dataset}.{field} targets another Odoo model"
    ),
    ExecutionSnapshotIssueCode.UNREVIEWED_BUSINESS_KEY: (
        "{dataset}.{field} is not expressed by a reviewed business key"
    ),
    ExecutionSnapshotIssueCode.MISSING_EXTERNAL_ID: (
        "Dataset {dataset} has a create without an External ID"
    ),
    ExecutionSnapshotIssueCode.REMOTE_MANY2MANY_CREATE_OPERATION: (
        "The remote-import slice supports replace/add for "
        "many2many creates; {dataset}.{field} is outside it"
    ),
    ExecutionSnapshotIssueCode.UNSUPPORTED_RELATIONSHIP_VALUE: (
        "{dataset}.{field} contains an unsupported relationship value"
    ),
    ExecutionSnapshotIssueCode.REMOTE_MANY2ONE_CREATE_OPERATION: (
        "The remote-import slice supports only one reviewed "
        "many2one create; {dataset}.{field} is outside it"
    ),
    ExecutionSnapshotIssueCode.UNSUPPORTED_RELATIONSHIP_OPERATION: (
        "{dataset}.{field} uses unsupported relationship operation {relation_operation}"
    ),
}


def execution_snapshot_message(issue: ExecutionSnapshotIssue | None) -> str:
    """Render one pure issue without changing validation order or evidence."""
    if issue is None:
        return ""
    if issue.code is ExecutionSnapshotIssueCode.NUMERIC_PRECISION_LOSS:
        if issue.precision is None:
            raise ValueError(
                "Numeric precision issues require captured summary evidence"
            )
        return issue.precision.message()
    return _SNAPSHOT_MESSAGES[issue.code].format(
        dataset=issue.dataset,
        field=issue.field,
        relation_operation=issue.relation_operation,
    )


def execution_scope_error_code(message: str) -> str:
    """Read the support code carried by an accepted controlled explanation."""
    marker = "Support code: "
    if marker not in message:
        return ""
    code = message.rsplit(marker, 1)[1].strip().removesuffix(".")
    return code if code.replace("_", "").isalnum() else ""
