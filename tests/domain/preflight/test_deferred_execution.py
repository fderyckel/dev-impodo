from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import unittest

from impodo.domain.execution_snapshot import (
    ExecutionDataset,
    ExecutionRow,
    ExecutionSnapshot,
    FieldIntent,
    plan_execution_rows,
)
from impodo.domain.preflight.deferred_execution import reduce_execution_snapshot
from impodo.domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredIssueScope,
    DeferredScopeEvidenceError,
    preview_deferred_scope,
)
from impodo.domain.shared.models import LogicalReference, canonical_json_bytes


HASH = "sha256:" + "a" * 64


def _row(
    token: int,
    *,
    dataset: str,
    disposition: str,
    fields: tuple[FieldIntent, ...] = (),
) -> ExecutionRow:
    return ExecutionRow(
        row_id="sha256:" + f"{token:064x}",
        dataset=dataset,
        source_row=token + 1,
        source_trace_id=f"trace-{token}",
        source_identity=(f"KEY-{token}",),
        target_model=f"x.{dataset}",
        business_identity=(f"KEY-{token}",),
        business_scope=(),
        disposition=disposition,
        target_match_count=0,
        proposed_external_id=(
            f"impodo_test.{dataset}_{token}" if disposition == "CREATE" else ""
        ),
        fields=fields,
    )


def _snapshot(rows: tuple[ExecutionRow, ...]) -> ExecutionSnapshot:
    dataset_names = tuple(sorted({row.dataset for row in rows}))
    datasets = tuple(
        ExecutionDataset(
            dataset=name,
            target_model=f"x.{name}",
            sequence=index,
            dependencies=(),
            existing_policy="update",
            identity_fields=("code",),
            scope_fields=(),
        )
        for index, name in enumerate(dataset_names)
    )
    planned_rows, relationship_plan = plan_execution_rows(rows, datasets)
    counts = {
        disposition: sum(row.disposition == disposition for row in planned_rows)
        for disposition in ("CREATE", "UPDATE", "UNCHANGED", "AMBIGUOUS", "BLOCKED")
    }
    root_hash = "sha256:" + sha256(
        canonical_json_bytes([row.row_hash for row in planned_rows])
    ).hexdigest()
    snapshot = ExecutionSnapshot(
        workspace_id="workspace-1",
        preflight_run_id="comparison-1",
        mapping_id="mapping-1",
        mapping_version=1,
        mapping_content_hash=HASH,
        compiled_plan_hash=HASH,
        staging_run_id="staging-1",
        staging_content_hash=HASH,
        quality_run_id="quality-1",
        quality_content_hash=HASH,
        normalization_run_id="normalization-1",
        normalization_content_hash=HASH,
        normalization_lifecycle_version=1,
        eligible_dataset_hash=HASH,
        frozen_input_hash=HASH,
        preflight_result_hash=HASH,
        metadata_snapshot_hash=HASH,
        record_snapshot_hash=HASH,
        target_hash=HASH,
        target_database="target",
        target_odoo_version="19.0",
        target_snapshot_at="2026-09-23T00:00:00+00:00",
        target_module_versions={},
        datasets=datasets,
        counts=counts,
        rows=planned_rows,
        root_hash=root_hash,
        relationship_plan=relationship_plan,
    )
    return ExecutionSnapshot.from_json(snapshot.to_json())


class DeferredExecutionTests(unittest.TestCase):
    def test_reduced_snapshot_removes_reviewed_problem_and_reconciles_counts(self):
        blocked = _row(1, dataset="rows", disposition="BLOCKED")
        first = _row(
            2,
            dataset="rows",
            disposition="CREATE",
            fields=(FieldIntent("name", "SET_VALUE", "First"),),
        )
        second = _row(
            3,
            dataset="rows",
            disposition="CREATE",
            fields=(FieldIntent("name", "SET_VALUE", "Second"),),
        )
        snapshot = _snapshot((blocked, first, second))
        issue = DeferredIssue(
            issue_id="sha256:" + "b" * 64,
            code="REFERENCE_NOT_FOUND",
            scope=DeferredIssueScope.ROW,
            row_id=blocked.row_id,
        )
        preview = preview_deferred_scope(
            comparison_id=snapshot.preflight_run_id,
            comparison_hash=snapshot.preflight_result_hash,
            execution_snapshot_hash=snapshot.semantic_hash,
            rows=snapshot.rows,
            issues=(issue,),
            dependencies=(),
            selected_issue_ids=(issue.issue_id,),
        )

        reduced = reduce_execution_snapshot(snapshot, preview)

        self.assertEqual({row.row_id for row in reduced.rows}, {first.row_id, second.row_id})
        self.assertEqual(reduced.write_count, 2)
        self.assertEqual(reduced.counts["BLOCKED"], 0)
        self.assertNotEqual(reduced.semantic_hash, snapshot.semantic_hash)

    def test_reduction_rejects_surviving_reference_to_omitted_parent(self):
        parent = _row(
            1,
            dataset="parents",
            disposition="CREATE",
            fields=(FieldIntent("name", "SET_VALUE", "Parent"),),
        )
        owner = _row(
            2,
            dataset="owners",
            disposition="CREATE",
            fields=(
                FieldIntent(
                    "parent_id",
                    "SET_VALUE",
                    LogicalReference(
                        origin="incoming",
                        key=parent.source_identity,
                        dataset=parent.dataset,
                    ),
                    kind="relation",
                    relation_operation="replace",
                    related_model=parent.target_model,
                    related_identity_fields=("name",),
                    dependency_strength="hard",
                ),
            ),
        )
        snapshot = _snapshot((parent, owner))
        issue = DeferredIssue(
            issue_id="sha256:" + "c" * 64,
            code="PARENT_INVALID",
            scope=DeferredIssueScope.ROW,
            row_id=parent.row_id,
        )
        preview = preview_deferred_scope(
            comparison_id=snapshot.preflight_run_id,
            comparison_hash=snapshot.preflight_result_hash,
            execution_snapshot_hash=snapshot.semantic_hash,
            rows=snapshot.rows,
            issues=(issue,),
            dependencies=(),
            selected_issue_ids=(issue.issue_id,),
        )

        with self.assertRaisesRegex(
            DeferredScopeEvidenceError,
            "relationship schedule",
        ):
            reduce_execution_snapshot(snapshot, preview)


if __name__ == "__main__":
    unittest.main()

