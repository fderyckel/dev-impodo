from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.application.workspace.derived_entities import (
    DerivedEntityWorkspaceService,
    ExactJoinRequest,
)
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    SourceDatasetUse,
    validate_saved_exact_join_reviews,
)
from impodo.domain.workspace.errors import WorkspaceError


WORKSPACE_ID = str(uuid4())


class _Plans:
    def __init__(
        self,
        current: DerivedEntityPlan | None = None,
        *,
        interrupt_once: bool = False,
    ) -> None:
        self.current = current
        self.interrupt_once = interrupt_once
        self.save_attempts = 0

    def get_derived_entity_plan(self, workspace_id: str) -> DerivedEntityPlan | None:
        assert workspace_id == WORKSPACE_ID
        return self.current

    def save_derived_entity_plan(
        self,
        workspace_id: str,
        plan: DerivedEntityPlan,
        *,
        expected_parent_version: int | None,
        actor: object,
    ) -> None:
        assert workspace_id == WORKSPACE_ID
        actual_parent = self.current.version if self.current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError("stale plan")
        self.save_attempts += 1
        if self.interrupt_once and self.save_attempts == 1:
            raise RuntimeError("simulated interrupted save")
        self.current = plan


class ExactJoinAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection, self.tables = _source_evidence()
        self.snapshots = tuple(
            Mock(
                dataset_id=item.dataset_id,
                logical_hash=f"sha256:{index:064x}",
                parquet_storage_key=f"source/{index}.parquet",
                parquet_sha256=f"{index:064x}",
            )
            for index, item in enumerate(self.selection.datasets, start=1)
        )
        self.sources = Mock()
        self.sources.get_source_selection.return_value = self.selection
        self.sources.get_current_source_snapshots.return_value = self.snapshots
        self.artifacts = Mock()
        self.artifacts.materialize_source_snapshot.return_value = nullcontext(
            Path("snapshot.parquet")
        )
        self.plans = _Plans()
        self.service = DerivedEntityWorkspaceService(
            self.sources,
            self.plans,
            CapabilityAuthorizationPolicy(),
            self.artifacts,
        )

    def _preview(self, request: ExactJoinRequest):
        with (
            patch(
                "impodo.application.workspace.derived_entities."
                "validate_snapshot_for_dataset"
            ),
            patch(
                "impodo.application.workspace.derived_entities."
                "load_source_snapshot_table",
                side_effect=lambda _path, snapshot: self.tables[snapshot.dataset_id],
            ),
        ):
            return self.service.preview_join(WORKSPACE_ID, request)

    def _save(
        self,
        request: ExactJoinRequest,
        *,
        expected_parent_version: int | None,
        dataset_uses=None,
    ):
        with (
            patch(
                "impodo.application.workspace.derived_entities."
                "validate_snapshot_for_dataset"
            ),
            patch(
                "impodo.application.workspace.derived_entities."
                "load_source_snapshot_table",
                side_effect=lambda _path, snapshot: self.tables[snapshot.dataset_id],
            ),
        ):
            return self.service.save_join(
                WORKSPACE_ID,
                request,
                dataset_uses=dataset_uses,
                expected_parent_version=expected_parent_version,
                actor=LOCAL_ACTOR,
            )

    def test_composite_optional_join_reports_complete_counts(self) -> None:
        _rule, review = self._preview(_customer_request())

        self.assertTrue(review.can_save)
        self.assertEqual(review.left_rows, 3)
        self.assertEqual(review.right_rows, 3)
        self.assertEqual(review.matched_left_rows, 2)
        self.assertEqual(review.unmatched_left_rows, 1)
        self.assertEqual(review.matched_right_rows, 2)
        self.assertEqual(review.unmatched_right_rows, 1)
        self.assertEqual(review.expected_output_rows, 3)
        self.assertEqual(review.actual_output_rows, 3)
        self.assertTrue(review.output_table_hash)
        self.assertTrue(review.lineage_hash)

    def test_required_join_returns_a_blocking_unmatched_finding(self) -> None:
        request = _customer_request(kind=JoinKind.INNER)

        _rule, review = self._preview(request)

        self.assertFalse(review.can_save)
        self.assertIn("Inner join has unmatched left rows", review.findings)
        self.assertIsNone(review.actual_output_rows)
        with self.assertRaisesRegex(WorkspaceError, "cannot be saved"):
            self._save(request, expected_parent_version=None)

    def test_duplicate_added_keys_and_blank_keys_are_reported_together(self) -> None:
        details = self.tables["dataset:details"]
        self.tables["dataset:details"] = SourceTable(
            dataset=details.dataset,
            path=details.path,
            headers=details.headers,
            rows=(
                SourceRow(1, {"Company": "A", "Customer": "C1", "Country": "BE"}),
                SourceRow(2, {"Company": "A", "Customer": "C1", "Country": "LU"}),
                SourceRow(3, {"Company": "", "Customer": "C3", "Country": "FR"}),
            ),
            content_hash=details.content_hash,
        )

        _rule, review = self._preview(_customer_request())

        self.assertFalse(review.can_save)
        self.assertEqual(review.duplicate_right_key_groups, 1)
        self.assertEqual(review.duplicate_right_key_rows, 2)
        self.assertEqual(review.blank_right_key_rows, 1)
        self.assertIn("Join right-side keys are not unique", review.findings)
        self.assertIn("Join key is blank on right-side rows", review.findings)

    def test_invalid_typed_keys_are_counted_on_each_side(self) -> None:
        request = _customer_request()
        request = ExactJoinRequest(
            output_dataset_name=request.output_dataset_name,
            left_dataset_id=request.left_dataset_id,
            right_dataset_id=request.right_dataset_id,
            keys=(JoinKey("company", "detail_company", "integer"),),
            output_columns=request.output_columns,
            projections=request.projections,
            require_all_right_rows=False,
        )

        _rule, review = self._preview(request)

        self.assertFalse(review.can_save)
        self.assertEqual(review.invalid_left_key_rows, 3)
        self.assertEqual(review.invalid_right_key_rows, 3)
        self.assertIn("Join key is invalid on left-side rows", review.findings)
        self.assertIn("Join key is invalid on right-side rows", review.findings)

    def test_output_column_name_collision_is_rejected_before_source_loading(self) -> None:
        request = _customer_request()
        colliding = ExactJoinRequest(
            output_dataset_name=request.output_dataset_name,
            left_dataset_id=request.left_dataset_id,
            right_dataset_id=request.right_dataset_id,
            keys=request.keys,
            output_columns=(
                StructuralOutputColumn("account", "Name", "string"),
                StructuralOutputColumn("name", "Name", "string"),
            ),
            projections=(
                StructuralProjection("account", "dataset:customers", "account"),
                StructuralProjection("name", "dataset:customers", "name"),
            ),
            require_all_right_rows=False,
        )

        with self.assertRaisesRegex(ValueError, "column names must be unique"):
            self._preview(colliding)
        self.sources.get_current_source_snapshots.assert_not_called()

    def test_join_can_use_the_output_of_an_existing_join(self) -> None:
        first = _customer_rule()
        self.plans.current = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
            source_selection_hash=self.selection.content_hash,
            rules=(first,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )
        first_output = structural_dataset_id(first)
        request = ExactJoinRequest(
            output_dataset_name="customer_complete",
            left_dataset_id=first_output,
            right_dataset_id="dataset:terms",
            keys=(JoinKey("account", "terms_account"),),
            output_columns=(
                StructuralOutputColumn("account", "Account", "string"),
                StructuralOutputColumn("country", "Country", "string"),
                StructuralOutputColumn("terms", "Terms", "string"),
            ),
            projections=(
                StructuralProjection("account", first_output, "account"),
                StructuralProjection("country", first_output, "country"),
                StructuralProjection("terms", "dataset:terms", "terms"),
            ),
            kind=JoinKind.LEFT,
            require_all_right_rows=True,
        )

        _rule, review = self._preview(request)

        self.assertTrue(review.can_save)
        self.assertEqual(review.left_rows, 3)
        self.assertEqual(review.matched_left_rows, 3)
        self.assertGreaterEqual(review.lineage_links, 7)
        self.assertEqual(
            {
                item.dataset_id
                for item in review.examples[0].physical_lineage
            },
            {"dataset:customers", "dataset:details", "dataset:terms"},
        )

    def test_save_persists_reproducible_review_and_complete_dataset_uses(self) -> None:
        uses = {
            item.dataset_id: SourceDatasetUse.PREPARATION_ONLY
            for item in self.selection.datasets[:2]
        }
        uses["dataset:terms"] = SourceDatasetUse.MAP_DIRECTLY

        request = _customer_request()
        preview_rule, preview = self._preview(request)
        plan, rule, review = self._save(
            request,
            expected_parent_version=None,
            dataset_uses=uses,
        )
        restored = DerivedEntityPlan.from_json(plan.to_json())

        self.assertTrue(review.can_save)
        self.assertEqual(rule.rule_id, preview_rule.rule_id)
        self.assertEqual(review.output_table_hash, preview.output_table_hash)
        self.assertEqual(plan.contract_version, 7)
        self.assertEqual(restored, plan)
        self.assertEqual(plan.join_reviews[0].rule_id, rule.rule_id)
        self.assertEqual(len(plan.join_reviews[0].input_snapshots), 3)
        self.assertEqual(plan.join_reviews[0].review.actual_output_rows, 3)

        incomplete = replace(
            plan,
            join_reviews=(
                replace(
                    plan.join_reviews[0],
                    input_snapshots=plan.join_reviews[0].input_snapshots[:-1],
                ),
            ),
        )
        with self.assertRaisesRegex(WorkspaceError, "snapshots do not match"):
            validate_saved_exact_join_reviews(self.selection, incomplete)

    def test_stale_version_blocks_before_source_materialization(self) -> None:
        self.plans.current = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=2,
            workspace_id=WORKSPACE_ID,
            source_selection_hash=self.selection.content_hash,
            rules=(),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )

        with self.assertRaisesRegex(WorkspaceError, "modified by another request"):
            self._save(_customer_request(), expected_parent_version=1)
        self.sources.get_current_source_snapshots.assert_not_called()

    def test_retry_after_interrupted_save_creates_one_plan_revision(self) -> None:
        self.plans.interrupt_once = True
        request = _customer_request()

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self._save(request, expected_parent_version=None)
        self.assertIsNone(self.plans.current)

        plan, _rule, _review = self._save(
            request,
            expected_parent_version=None,
        )

        self.assertEqual(plan.version, 1)
        self.assertEqual(len(plan.rules), 1)
        self.assertEqual(len(plan.join_reviews), 1)
        self.assertEqual(self.plans.save_attempts, 2)


def _customer_request(*, kind: JoinKind = JoinKind.LEFT) -> ExactJoinRequest:
    return ExactJoinRequest(
        output_dataset_name="customers_complete",
        left_dataset_id="dataset:customers",
        right_dataset_id="dataset:details",
        keys=(
            JoinKey("company", "detail_company"),
            JoinKey("account", "detail_account"),
        ),
        output_columns=(
            StructuralOutputColumn("account", "Account", "string"),
            StructuralOutputColumn("name", "Name", "string"),
            StructuralOutputColumn("country", "Country", "string"),
        ),
        projections=(
            StructuralProjection("account", "dataset:customers", "account"),
            StructuralProjection("name", "dataset:customers", "name"),
            StructuralProjection("country", "dataset:details", "country"),
        ),
        kind=kind,
        require_all_right_rows=False,
    )


def _customer_rule() -> ExactJoinRule:
    request = _customer_request()
    return ExactJoinRule(
        rule_id=str(uuid4()),
        output_dataset_name=request.output_dataset_name,
        left_dataset_id=request.left_dataset_id,
        right_dataset_id=request.right_dataset_id,
        keys=request.keys,
        output_columns=request.output_columns,
        projections=request.projections,
        kind=request.kind,
        require_all_right_rows=request.require_all_right_rows,
    )


def _source_evidence() -> tuple[SourceSelection, dict[str, SourceTable]]:
    now = datetime.now(timezone.utc)
    definitions = (
        (
            "dataset:customers",
            "customers",
            (
                SourceDatasetColumn(1, "Company", "company", "string"),
                SourceDatasetColumn(2, "Account", "account", "string"),
                SourceDatasetColumn(3, "Name", "name", "string"),
            ),
            (
                SourceRow(1, {"Company": "A", "Account": "C1", "Name": "Alpha"}),
                SourceRow(2, {"Company": "A", "Account": "C2", "Name": "Beta"}),
                SourceRow(3, {"Company": "B", "Account": "C1", "Name": "Gamma"}),
            ),
        ),
        (
            "dataset:details",
            "details",
            (
                SourceDatasetColumn(1, "Company", "detail_company", "string"),
                SourceDatasetColumn(2, "Customer", "detail_account", "string"),
                SourceDatasetColumn(3, "Country", "country", "string"),
            ),
            (
                SourceRow(1, {"Company": "A", "Customer": "C1", "Country": "BE"}),
                SourceRow(2, {"Company": "B", "Customer": "C1", "Country": "LU"}),
                SourceRow(3, {"Company": "A", "Customer": "C9", "Country": "FR"}),
            ),
        ),
        (
            "dataset:terms",
            "terms",
            (
                SourceDatasetColumn(1, "Customer", "terms_account", "string"),
                SourceDatasetColumn(2, "Terms", "terms", "string"),
            ),
            (
                SourceRow(1, {"Customer": "C1", "Terms": "30D"}),
                SourceRow(2, {"Customer": "C2", "Terms": "45D"}),
            ),
        ),
    )
    datasets = tuple(
        SourceDataset(
            dataset_id=dataset_id,
            name=name,
            source=FileSourceBinding(
                file_id=str(uuid4()),
                table_key=f"{name}.csv",
                source_sha256=f"{index:064x}",
                catalog_hash=f"sha256:{index + 10:064x}",
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            row_count=len(rows),
            columns=columns,
        )
        for index, (dataset_id, name, columns, rows) in enumerate(definitions, start=1)
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=now,
        created_by="Test operator",
        datasets=datasets,
        content_hash="sha256:" + "a" * 64,
    )
    tables = {
        dataset_id: SourceTable(
            dataset=name,
            path=Path(f"{name}.csv"),
            headers=tuple(item.source_name for item in columns),
            rows=rows,
            content_hash=f"sha256:{index + 20:064x}",
        )
        for index, (dataset_id, name, columns, rows) in enumerate(definitions, start=1)
    }
    return selection, tables


if __name__ == "__main__":
    unittest.main()
