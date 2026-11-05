"""Keep child source rows in step with an explicitly selected incoming parent."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from starlette.datastructures import FormData

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    RowInclusionCondition,
    RowInclusionMode,
    RowInclusionPolicy,
    SelectionConditionOperator,
)
from impodo.web.presenters.mapping_forms import _row_inclusion_from_form
from tests.integration.web.test_mapping_form_roundtrip import _RenderedControls
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_cases import CATEGORY_ROWS
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class IncludedParentRowsTests(HierarchyWorkflowFixture):
    def test_rows_review_and_preparation_agree_for_both_majors_and_access_modes(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._review_and_prepare(major, mode)

    def _review_and_prepare(self, major, mode):
        workspace_id, revision = self.hierarchy(
            major=major, mode=mode, source_shape="supplied"
        )
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        product_index, products = next(
            (index, dataset) for index, dataset in enumerate(selection.datasets)
            if dataset.name == "products"
        )
        page_url = f"/workspaces/{workspace_id}/mapping?mapping_dataset={product_index}"
        with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
            page = self.client.get(page_url)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Use only rows whose parent is included", page.text)
        self.assertIn("· categories", page.text)
        self.assertLessEqual(timings.connection_count, 8)
        # Access, readiness, and page scopes check the workspace three times,
        # the engine twice, and the selected Data version once.
        self.assertLessEqual(timings.schema_check_count, 6)
        self.assertLessEqual(len(statements), 140)
        self.assertEqual(
            sum("duckdb_columns()" in statement for statement in statements),
            timings.schema_check_count,
        )
        self.assertEqual(timings.lock_retry_count, 0)
        policy = _row_inclusion_from_form(
            FormData((
                (f"row_inclusion_mode_{product_index}", "included_parent"),
                (f"row_inclusion_parent_{product_index}", "categ_id"),
            )),
            dataset_index=product_index,
            source_columns={column.stable_key for column in products.columns},
        )
        parents = next(
            item for item in revision.definition.datasets
            if item.target_model == "product.category"
        )
        mappings = tuple(
            replace(item, row_inclusion=policy)
            if item.dataset_id == products.dataset_id
            else replace(
                item,
                row_inclusion=RowInclusionPolicy(
                    mode=RowInclusionMode.MATCHING_ROWS,
                    conditions=(RowInclusionCondition(
                        str(uuid4()), parents.source_identity_column_keys[0],
                        SelectionConditionOperator.NOT_EQUALS, "C300",
                    ),),
                ),
            )
            for item in revision.definition.datasets
        )
        if mode == "REMOTE":
            # Exercise the large-table reader with the same small, inspectable data.
            with patch(
                "impodo.application.workspace.mapping.bounded_direct_review."
                "MATERIALIZED_BROWSER_EVALUATION_ROW_LIMIT",
                0,
            ):
                checked = self.submit(workspace_id, mappings, confirm_rows=True)
        else:
            checked = self.submit(workspace_id, mappings, confirm_rows=True)
        roundtrip = self.context.queries.get_mapping_revision(workspace_id)
        self.assertEqual(checked.definition.content_hash, roundtrip.definition.content_hash)
        page = self.client.get(page_url)
        controls = _RenderedControls()
        controls.feed(page.text)
        self.assertIn((f"row_inclusion_mode_{product_index}", "included_parent"), controls.entries)
        self.assertIn((f"row_inclusion_parent_{product_index}", "categ_id"), controls.entries)

        snapshot, confirmation = self.context.row_inclusion_reviews.current(
            workspace_id, actor=self.actor,
        )
        result = next(item for item in snapshot.datasets if item.dataset_id == products.dataset_id)
        self.assertEqual((result.included_count, result.excluded_count, result.cannot_evaluate_count), (5, 1, 0))
        self.assertIsNotNone(confirmation)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.quarantined_count, 0)
        self.assertEqual(quality.excluded_count, 2)
        self.assertEqual(
            {
                row.source_identity for row in staging.rows
                if row.target_model == "product.template" and row.source_identity
            },
            {(f"PX10{index}",) for index in range(1, 6)},
        )

    def test_check_matches_flags_duplicate_parent_source_identifiers(self):
        workspace_id, revision = self.hierarchy(
            source_shape="supplied",
            categories=(*CATEGORY_ROWS, CATEGORY_ROWS[-1]),
        )
        policy = RowInclusionPolicy(
            mode=RowInclusionMode.MATCHING_ROWS, included_parent_field="categ_id",
        )
        validation = self.submit(
            workspace_id,
            tuple(
                replace(item, row_inclusion=policy) if item.target_model == "product.template" else item
                for item in revision.definition.datasets
            ),
            check_only=True,
        )
        self.assertEqual(validation.status.value, "INVALID")
        self.assertIn("MAPPING_INCLUDED_PARENT_AMBIGUOUS", {item.code for item in validation.issues})
