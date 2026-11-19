"""Verify continuation across independent generated product and BOM receipts."""

import unittest
from dataclasses import replace
from itertools import product
from unittest.mock import patch

from impodo.domain.execution.load_control import RejectionPolicy
from impodo.domain.execution.models import ExecutionRowStatus, ExecutionRunStatus
from impodo.domain.execution.odoo_scope import execution_api_scope
from impodo.domain.execution_snapshot import FieldIntent
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import (
    test_continuation as continuation,
    test_service as fixtures,
)


def _product_bom_snapshot(major):
    template = fixtures._generated_variant_snapshot()
    product_row, line_row = template.rows
    products = []
    lines = []
    for index, name in enumerate(("Rejected", "Second", "Third"), start=1):
        code = f"P{index}"
        products.append(
            fixtures._row(
                dataset=product_row.dataset,
                model=product_row.target_model,
                source_row=index,
                source_identity=(code,),
                business_identity=(code,),
                disposition="CREATE",
                fields=(
                    FieldIntent("default_code", "SET_VALUE", code),
                    FieldIntent("name", "SET_VALUE", name),
                ),
            )
        )
        (relation,) = line_row.fields
        lines.append(
            fixtures._row(
                dataset=line_row.dataset,
                model=line_row.target_model,
                source_row=10 + index,
                source_identity=("BOM", code),
                business_identity=("BOM", code),
                disposition="CREATE",
                fields=(replace(relation, value=replace(relation.value, key=(code,))),),
            )
        )
    return fixtures._with_plan(
        replace(
            template,
            rows=(*products, *lines),
            counts={**template.counts, "CREATE": 6},
            target_odoo_version=f"{major}.0",
            target_module_versions={"base": f"{major}.0.1.0"},
        )
    )


class ProjectedRejectionTests(unittest.TestCase):
    def test_rejected_product_blocks_only_its_bom_line(self):
        for major, mode in product(
            (19, 20), (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE)
        ):
            with self.subTest(major=major, mode=mode):
                snapshot = _product_bom_snapshot(major)
                service, journal = continuation.ContinuationTests()._service(
                    snapshot, mode
                )
                writer = continuation._Writer(
                    execution_api_scope(snapshot).semantic_hash, {"Rejected"}
                )
                result = service.execute(
                    snapshot.workspace_id,
                    expected_snapshot_hash=snapshot.semantic_hash,
                    executor=writer,
                    actor=LOCAL_ACTOR,
                    rejection_policy=RejectionPolicy.CONTINUE,
                )
                self.assertEqual(
                    result.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS
                )
                self.assertEqual(result.rows, journal.run.rows)
                self.assertEqual(
                    [row.status for row in result.rows],
                    [
                        ExecutionRowStatus.FAILED,
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.BLOCKED,
                        ExecutionRowStatus.COMMITTED,
                        ExecutionRowStatus.COMMITTED,
                    ],
                )
                self.assertEqual(
                    writer.projection_reads,
                    [
                        (
                            "product.template",
                            (10, 11),
                            "product_variant_id",
                            "product.product",
                        )
                    ],
                )
                self.assertEqual(
                    [
                        [
                            (r.projection_field, r.target_model, r.odoo_id)
                            for r in row.projected_receipts
                        ]
                        for row in result.rows[:3]
                    ],
                    [
                        [],
                        [("product_variant_id", "product.product", 110)],
                        [("product_variant_id", "product.product", 111)],
                    ],
                )
                writes = (
                    writer.loads
                    if mode is OdooConnectionMode.REMOTE
                    else writer.creates
                )
                self.assertEqual(
                    [
                        row
                        for model, rows, *_ in writes
                        if model == "mrp.bom.line"
                        for row in rows
                    ],
                    [{"product_id/.id": "110"}, {"product_id/.id": "111"}]
                    if mode is OdooConnectionMode.REMOTE
                    else [{"product_id": 110}, {"product_id": 111}],
                )

    def test_incomplete_read_back_keeps_unproven_bom_lines_blocked(self):
        snapshot = _product_bom_snapshot(20)
        service, _ = continuation.ContinuationTests()._service(
            snapshot, OdooConnectionMode.REMOTE
        )
        writer = continuation._Writer(
            execution_api_scope(snapshot).semantic_hash, {"Rejected"}
        )
        with patch.object(writer, "read_projected_ids", return_value=(110,)) as read:
            result = service.execute(
                snapshot.workspace_id,
                expected_snapshot_hash=snapshot.semantic_hash,
                executor=writer,
                actor=LOCAL_ACTOR,
                rejection_policy=RejectionPolicy.CONTINUE,
            )
        read.assert_called_once_with(
            "product.template", (10, 11), "product_variant_id", "product.product"
        )
        self.assertEqual(result.status, ExecutionRunStatus.COMPLETED_WITH_ERRORS)
        self.assertEqual(
            [row.status for row in result.rows],
            [
                ExecutionRowStatus.FAILED,
                ExecutionRowStatus.PARTIALLY_APPLIED,
                ExecutionRowStatus.PARTIALLY_APPLIED,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.BLOCKED,
                ExecutionRowStatus.BLOCKED,
            ],
        )
        self.assertTrue(all(not row.projected_receipts for row in result.rows))
        self.assertFalse(any(model == "mrp.bom.line" for model, *_ in writer.loads))
