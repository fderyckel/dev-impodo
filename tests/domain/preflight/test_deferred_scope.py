from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import unittest

from impodo.domain.preflight.deferred_scope import (
    DeferredIssue,
    DeferredIssueScope,
    DeferredScopeEvidenceError,
    preflight_deferred_issues,
    prepared_dependency_facts,
    preview_deferred_scope,
)
from impodo.domain.shared.models import (
    Classification,
    Decision,
    Issue,
    LogicalReference,
    PreflightResult,
    PreparedRecord,
    ReferenceResolution,
    Severity,
    TargetFingerprint,
)


HASH = "sha256:" + "a" * 64
SNAPSHOT_HASH = "sha256:" + "b" * 64
COMPARISON_ID = "comparison-1"


@dataclass(frozen=True, slots=True)
class _ExecutionRow:
    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    source_identity: tuple[object, ...]
    target_model: str
    disposition: str


@dataclass(frozen=True, slots=True)
class _RelationshipBlocker:
    row_id: str
    code: str
    field: str = ""


def _row(
    token: str,
    dataset: str,
    source_row: int,
    source_identity: tuple[object, ...],
    disposition: str,
) -> _ExecutionRow:
    digest = "sha256:" + token * 64
    return _ExecutionRow(
        row_id=digest,
        dataset=dataset,
        source_row=source_row,
        source_trace_id=f"trace-{token}",
        source_identity=source_identity,
        target_model=f"x.{dataset}",
        disposition=disposition,
    )


def _record(
    row: _ExecutionRow,
    *,
    target_identity: tuple[object, ...] | None = None,
    references: dict[str, object] | None = None,
) -> PreparedRecord:
    return PreparedRecord(
        dataset=row.dataset,
        source_row=row.source_row,
        source_trace_id=row.source_trace_id,
        target_model=row.target_model,
        source_identity=row.source_identity,
        target_identity=(
            target_identity if target_identity is not None else row.source_identity
        ),
        target_scope=(),
        scalar_values={},
        references=references or {},
    )


def _result(decisions, resolutions=(), issues=()) -> PreflightResult:
    return PreflightResult(
        profile_id="browser",
        source_hashes={},
        fingerprint=TargetFingerprint(
            target_hash="sha256:" + "c" * 64,
            connection_mode="REMOTE",
            database="target",
            odoo_version="19.0",
            snapshot_timestamp=datetime(2026, 9, 23, tzinfo=timezone.utc).isoformat(),
        ),
        metadata_snapshot_hash="sha256:" + "d" * 64,
        record_snapshot_hash="sha256:" + "e" * 64,
        decisions=tuple(decisions),
        reference_resolutions=tuple(resolutions),
        issues=tuple(issues),
    )


class DeferredScopeTests(unittest.TestCase):
    def test_missing_component_defers_complete_bom_group_and_keeps_other_group(self):
        bom_1 = _row("1", "boms", 2, ("BOM-1",), "CREATE")
        bad_line = _row("2", "bom_lines", 2, ("BOM-1", "A"), "BLOCKED")
        sibling = _row("3", "bom_lines", 3, ("BOM-1", "B"), "CREATE")
        bom_2 = _row("4", "boms", 3, ("BOM-2",), "CREATE")
        safe_line = _row("5", "bom_lines", 4, ("BOM-2", "C"), "CREATE")
        rows = (bom_1, bad_line, sibling, bom_2, safe_line)

        def bom_reference(key: str) -> LogicalReference:
            return LogicalReference(
                origin="incoming",
                key=(key,),
                dataset="boms",
            )

        records = (
            _record(bom_1),
            _record(bad_line, target_identity=(bom_reference("BOM-1"), "A")),
            _record(sibling, target_identity=(bom_reference("BOM-1"), "B")),
            _record(bom_2),
            _record(safe_line, target_identity=(bom_reference("BOM-2"), "C")),
        )
        result = _result(
            (
                Decision(
                    "boms",
                    2,
                    ("BOM-1",),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=bom_1.source_trace_id,
                ),
                Decision(
                    "bom_lines",
                    2,
                    ("BOM-1", "A"),
                    (),
                    Classification.BLOCKED,
                    0,
                    source_trace_id=bad_line.source_trace_id,
                    issues=(
                        Issue(
                            code="REFERENCE_NOT_FOUND",
                            message="The component product is missing in Odoo.",
                            severity=Severity.ERROR,
                            dataset="bom_lines",
                            row=2,
                            field="product_id",
                        ),
                    ),
                ),
                Decision(
                    "bom_lines",
                    3,
                    ("BOM-1", "B"),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=sibling.source_trace_id,
                ),
                Decision(
                    "boms",
                    3,
                    ("BOM-2",),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=bom_2.source_trace_id,
                ),
                Decision(
                    "bom_lines",
                    4,
                    ("BOM-2", "C"),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=safe_line.source_trace_id,
                ),
            )
        )

        issues = preflight_deferred_issues(COMPARISON_ID, rows, result)
        dependencies = prepared_dependency_facts(rows, records, ())
        preview = preview_deferred_scope(
            comparison_id=COMPARISON_ID,
            comparison_hash=HASH,
            execution_snapshot_hash=SNAPSHOT_HASH,
            rows=rows,
            issues=issues,
            dependencies=dependencies,
            selected_issue_ids=(issues[0].issue_id,),
            already_set_aside_count=7,
        )

        self.assertEqual(
            {item.row_id for item in preview.omitted_rows},
            {bom_1.row_id, bad_line.row_id, sibling.row_id},
        )
        self.assertEqual(dict(preview.counts_by_dataset), {"bom_lines": 2, "boms": 1})
        self.assertEqual(preview.prepared_record_count, 12)
        self.assertEqual(preview.already_set_aside_count, 7)
        self.assertEqual(preview.original_write_count, 4)
        self.assertEqual(preview.omitted_write_count, 2)
        self.assertEqual(preview.remaining_write_count, 2)
        self.assertEqual(preview.remaining_problem_record_count, 0)
        self.assertTrue(preview.ready_with_records_set_aside)
        direct = next(item for item in preview.omitted_rows if item.row_id == bad_line.row_id)
        inherited = next(item for item in preview.omitted_rows if item.row_id == sibling.row_id)
        self.assertTrue(direct.direct)
        self.assertFalse(inherited.direct)
        self.assertEqual(inherited.inherited_issue_ids, (issues[0].issue_id,))
        self.assertEqual(
            preview.semantic_hash,
            replace(preview).semantic_hash,
        )
        reordered = preview_deferred_scope(
            comparison_id=COMPARISON_ID,
            comparison_hash=HASH,
            execution_snapshot_hash=SNAPSHOT_HASH,
            rows=tuple(reversed(rows)),
            issues=tuple(reversed(issues)),
            dependencies=tuple(reversed(dependencies)),
            selected_issue_ids=(issues[0].issue_id,),
            already_set_aside_count=7,
        )
        self.assertEqual(preview.semantic_hash, reordered.semantic_hash)

    def test_ordinary_lookup_propagates_parent_failure_forward_only(self):
        product = _row("1", "products", 2, ("P-1",), "BLOCKED")
        bom_line = _row("2", "bom_lines", 2, ("BOM-1", "P-1"), "CREATE")
        lookup = LogicalReference(
            origin="incoming",
            key=("P-1",),
            dataset="products",
        )
        rows = (product, bom_line)
        records = (
            _record(product),
            _record(bom_line, references={"product_id": lookup}),
        )
        dependencies = prepared_dependency_facts(rows, records, ())
        product_issue = DeferredIssue(
            issue_id="sha256:" + "6" * 64,
            code="PRODUCT_INVALID",
            scope=DeferredIssueScope.ROW,
            row_id=product.row_id,
        )
        line_issue = DeferredIssue(
            issue_id="sha256:" + "7" * 64,
            code="LINE_INVALID",
            scope=DeferredIssueScope.ROW,
            row_id=bom_line.row_id,
        )

        parent_preview = preview_deferred_scope(
            comparison_id=COMPARISON_ID,
            comparison_hash=HASH,
            execution_snapshot_hash=SNAPSHOT_HASH,
            rows=rows,
            issues=(product_issue,),
            dependencies=dependencies,
            selected_issue_ids=(product_issue.issue_id,),
        )
        line_preview = preview_deferred_scope(
            comparison_id=COMPARISON_ID,
            comparison_hash=HASH,
            execution_snapshot_hash=SNAPSHOT_HASH,
            rows=(replace(product, disposition="CREATE"), bom_line),
            issues=(line_issue,),
            dependencies=dependencies,
            selected_issue_ids=(line_issue.issue_id,),
        )

        self.assertEqual(
            {item.row_id for item in parent_preview.omitted_rows},
            {product.row_id, bom_line.row_id},
        )
        self.assertEqual(
            {item.row_id for item in line_preview.omitted_rows},
            {bom_line.row_id},
        )

    def test_hybrid_reference_uses_incoming_dependency_only_when_comparison_did(self):
        parent = _row("1", "products", 2, ("P-1",), "CREATE")
        owner = _row("2", "bom_lines", 2, ("P-1", "A"), "CREATE")
        hybrid = LogicalReference(
            origin="target_then_incoming",
            key=("P-1",),
            dataset="products",
            model="product.product",
            target_fields=("default_code",),
            incoming_key=("P-1",),
        )
        records = (
            _record(parent),
            _record(owner, target_identity=(hybrid, "A")),
        )

        incoming = prepared_dependency_facts(
            (parent, owner),
            records,
            (
                ReferenceResolution(
                    dataset="bom_lines",
                    field="target_identity:bom_id",
                    reference=hybrid,
                    status="RESOLVED_INCOMING",
                    match_count=1,
                ),
            ),
        )
        target = prepared_dependency_facts(
            (parent, owner),
            records,
            (
                ReferenceResolution(
                    dataset="bom_lines",
                    field="target_identity:bom_id",
                    reference=hybrid,
                    status="RESOLVED_TARGET",
                    match_count=1,
                ),
            ),
        )

        self.assertEqual(len(incoming), 1)
        self.assertTrue(incoming[0].keeps_group_together)
        self.assertEqual(target, ())

    def test_run_issue_and_unselected_row_keep_reduced_scope_blocked(self):
        first = _row("1", "rows", 2, ("A",), "BLOCKED")
        second = _row("2", "rows", 3, ("B",), "BLOCKED")
        write = _row("3", "rows", 4, ("C",), "CREATE")
        first_issue = DeferredIssue(
            issue_id="sha256:" + "4" * 64,
            code="FIRST",
            scope=DeferredIssueScope.ROW,
            row_id=first.row_id,
        )
        second_issue = DeferredIssue(
            issue_id="sha256:" + "5" * 64,
            code="SECOND",
            scope=DeferredIssueScope.ROW,
            row_id=second.row_id,
        )
        run_issue = DeferredIssue(
            issue_id="sha256:" + "6" * 64,
            code="TARGET_UNAVAILABLE",
            scope=DeferredIssueScope.RUN,
        )

        preview = preview_deferred_scope(
            comparison_id=COMPARISON_ID,
            comparison_hash=HASH,
            execution_snapshot_hash=SNAPSHOT_HASH,
            rows=(first, second, write),
            issues=(first_issue, second_issue, run_issue),
            dependencies=(),
            selected_issue_ids=(first_issue.issue_id,),
        )

        self.assertEqual(preview.remaining_problem_record_count, 1)
        self.assertEqual(preview.remaining_run_issue_count, 1)
        self.assertFalse(preview.ready_with_records_set_aside)
        with self.assertRaisesRegex(
            DeferredScopeEvidenceError,
            "run-wide failure",
        ):
            preview_deferred_scope(
                comparison_id=COMPARISON_ID,
                comparison_hash=HASH,
                execution_snapshot_hash=SNAPSHOT_HASH,
                rows=(first, second, write),
                issues=(first_issue, second_issue, run_issue),
                dependencies=(),
                selected_issue_ids=(run_issue.issue_id,),
            )

    def test_relationship_schedule_blocker_becomes_row_issue(self):
        row = _row("1", "rows", 2, ("A",), "CREATE")
        result = _result(
            (
                Decision(
                    "rows",
                    2,
                    ("A",),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=row.source_trace_id,
                ),
            )
        )

        issues = preflight_deferred_issues(
            COMPARISON_ID,
            (row,),
            result,
            relationship_blockers=(
                _RelationshipBlocker(
                    row_id=row.row_id,
                    code="MISSING_INCOMING_ROW",
                    field="parent_id",
                ),
            ),
        )

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "RELATIONSHIP_MISSING_INCOMING_ROW")
        self.assertEqual(issues[0].field, "parent_id")

    def test_incomplete_comparison_decision_accounting_fails_closed(self):
        first = _row("1", "rows", 2, ("A",), "CREATE")
        second = _row("2", "rows", 3, ("B",), "CREATE")
        result = _result(
            (
                Decision(
                    "rows",
                    2,
                    ("A",),
                    (),
                    Classification.CREATE,
                    0,
                    source_trace_id=first.source_trace_id,
                ),
            )
        )

        with self.assertRaisesRegex(
            DeferredScopeEvidenceError,
            "accounting is incomplete",
        ):
            preflight_deferred_issues(COMPARISON_ID, (first, second), result)

    def test_incomplete_dependency_evidence_fails_closed(self):
        owner = _row("1", "owners", 2, ("A",), "BLOCKED")
        missing = LogicalReference(
            origin="incoming",
            key=("missing",),
            dataset="parents",
        )
        with self.assertRaisesRegex(
            DeferredScopeEvidenceError,
            "missing or ambiguous",
        ):
            prepared_dependency_facts(
                (owner,),
                (_record(owner, target_identity=(missing,)),),
                (),
            )


if __name__ == "__main__":
    unittest.main()
