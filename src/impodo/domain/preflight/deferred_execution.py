"""Derive one reduced execution snapshot from a reviewed local scope.

This module never reads Odoo or preparation sources.  It removes only the
rows named by a verified deferred-scope preview, recalculates the relationship
schedule from the frozen field intents, and rejects any unsafe remainder.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from impodo.domain.execution_snapshot import (
    ExecutionSnapshot,
    plan_execution_rows,
)
from impodo.domain.shared.models import Classification, canonical_json_bytes

from .deferred_scope import DeferredScopeEvidenceError, DeferredScopePreview


def reduce_execution_snapshot(
    snapshot: ExecutionSnapshot,
    preview: DeferredScopePreview,
) -> ExecutionSnapshot:
    """Return the exact locally rescheduled remainder accepted by a preview."""

    if (
        not preview.ready_with_records_set_aside
        or preview.comparison_id != snapshot.preflight_run_id
        or preview.execution_snapshot_hash != snapshot.semantic_hash
    ):
        raise DeferredScopeEvidenceError(
            "The deferred preview does not match the full execution snapshot"
        )
    omitted_ids = {item.row_id for item in preview.omitted_rows}
    row_ids = {item.row_id for item in snapshot.rows}
    if not omitted_ids or not omitted_ids.issubset(row_ids):
        raise DeferredScopeEvidenceError(
            "The deferred preview names unknown execution rows"
        )
    retained = tuple(row for row in snapshot.rows if row.row_id not in omitted_ids)
    rows, relationship_plan = plan_execution_rows(retained, snapshot.datasets)
    if relationship_plan.blockers:
        raise DeferredScopeEvidenceError(
            "The remaining relationship schedule is not safe to load"
        )
    counts = {
        classification.value: sum(
            row.disposition == classification.value for row in rows
        )
        for classification in Classification
    }
    write_count = counts[Classification.CREATE.value] + counts[
        Classification.UPDATE.value
    ]
    if write_count != preview.remaining_write_count:
        raise DeferredScopeEvidenceError(
            "The reduced execution write count does not match the reviewed preview"
        )
    reduced = replace(
        snapshot,
        counts=counts,
        rows=rows,
        root_hash="sha256:"
        + sha256(canonical_json_bytes([row.row_hash for row in rows])).hexdigest(),
        relationship_plan=relationship_plan,
    )
    # Round-tripping executes the complete portable snapshot validator and
    # proves the locally rebuilt schedule without relying on private helpers.
    return ExecutionSnapshot.from_json(reduced.to_json())

