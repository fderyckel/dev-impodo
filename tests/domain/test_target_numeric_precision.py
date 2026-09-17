from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
import unittest

from impodo.application.workspace.preparation.preparation_service import (
    prepared_target_precision_error,
)
from impodo.application.odoo_comparison_service import _same_write_field
from impodo.domain.mapping.contracts import MappingTargetMode, ScalarValueSource
from impodo.domain.shared.models import FieldMetadata
from impodo.domain.target_numeric_precision import (
    TargetNumericPrecisionLosses,
    required_decimal_places,
    unrepresentable_decimal,
)
from impodo.domain.workspace.contracts import SchemaField


class TargetNumericPrecisionTests(unittest.TestCase):
    def test_each_odoo_float_uses_its_own_decimal_scale(self) -> None:
        for scale in (2, 6, 10):
            with self.subTest(scale=scale):
                exact = Decimal("0." + "1" * scale)
                excess = Decimal("0." + "1" * (scale + 1))
                self.assertIsNone(unrepresentable_decimal(exact, (16, scale)))
                self.assertEqual(unrepresentable_decimal(excess, (16, scale)), excess)
                losses = TargetNumericPrecisionLosses()
                losses.add("x.model", "x_quantity", (16, scale), excess)
                self.assertIn(f"precision (16, {scale})", losses.first_message())

    def test_positive_values_that_round_to_zero_are_reported(self) -> None:
        losses = TargetNumericPrecisionLosses()
        losses.add("mrp.bom.line", "product_qty", (16, 2), Decimal("0.003"))
        self.assertIn("1 positive value(s) into zero", losses.first_message())

    def test_long_values_and_exponents_do_not_use_decimal_context_precision(self) -> None:
        long_value = Decimal("0." + "1" * 30)
        self.assertEqual(required_decimal_places(long_value), 30)
        losses = TargetNumericPrecisionLosses()
        losses.add("x.model", "x_quantity", (16, 2), long_value)
        self.assertIn("at least 30 decimal places", losses.first_message())
        self.assertEqual(required_decimal_places(Decimal("1.230000")), 2)
        self.assertIsNone(unrepresentable_decimal(Decimal("1E+20"), (24, 2)))
        self.assertEqual(
            unrepresentable_decimal(Decimal("1E+20"), (16, 2)),
            Decimal("1E+20"),
        )
        self.assertIsNone(unrepresentable_decimal(
            Decimal("1234567890123456789012345678"), (38, 2),
        ))

    def test_changed_odoo_float_precision_invalidates_captured_write_field(self) -> None:
        captured = SchemaField(
            name="x_quantity", label="Quantity", type="float",
            required=False, readonly=False, relation=None, relation_field=None,
            selection=(), digits=(16, 6),
        )
        live = FieldMetadata(
            name="x_quantity", label="Quantity", type="float",
            required=False, readonly=False, relation=None, relation_field=None,
            selection=(), digits=(16, 10),
        )
        self.assertFalse(_same_write_field(captured, live))

    def test_early_check_respects_exclusions_and_write_fields(self) -> None:
        row = lambda row_id, values: SimpleNamespace(
            row_id=row_id,
            dataset="lines",
            target_model="x.model",
            proposed_values=values,
        )
        rows = (
            row("included", {
                "x_two": Decimal("0.12"),
                "x_six": Decimal("0.123456"),
                "x_unbounded": Decimal("0.12345678901234567890"),
            }),
            row("excluded", {"x_two": Decimal("0.003")}),
            row("check_only", {"x_two": Decimal("0.12"), "x_check": Decimal("0.123")}),
        )
        definition = SimpleNamespace(datasets=(SimpleNamespace(
            dataset_id="line-id",
            mode=MappingTargetMode.UPSERT,
            fields=(
                SimpleNamespace(target_field="x_two", validate_only=False, value_source=ScalarValueSource.SOURCE),
                SimpleNamespace(target_field="x_six", validate_only=False, value_source=ScalarValueSource.SOURCE),
                SimpleNamespace(target_field="x_check", validate_only=True, value_source=ScalarValueSource.SOURCE),
                SimpleNamespace(target_field="x_unbounded", validate_only=False, value_source=ScalarValueSource.SOURCE),
            ),
        ),))
        selection = SimpleNamespace(datasets=(SimpleNamespace(dataset_id="line-id", name="lines"),))
        schema = SimpleNamespace(models=(SimpleNamespace(
            name="x.model",
            fields=(
                SimpleNamespace(name="x_two", type="float", digits=(16, 2)),
                SimpleNamespace(name="x_six", type="float", digits=(16, 6)),
                SimpleNamespace(name="x_check", type="float", digits=(16, 2)),
                SimpleNamespace(name="x_unbounded", type="float", digits=None),
            ),
        ),))

        self.assertIsNone(prepared_target_precision_error(
            rows, {"included", "check_only"}, definition, selection, schema
        ))
        message = prepared_target_precision_error(
            rows, {"included", "excluded", "check_only"}, definition, selection, schema
        )
        self.assertIn("x.model.x_two", message)
        self.assertIn("1 positive value(s) into zero", message)

    def test_early_check_uses_worker_precision_packet_without_local_schema(self) -> None:
        row = SimpleNamespace(
            row_id="one", dataset="lines", target_model="x.model",
            proposed_values={"x_ten": Decimal("0.12345678901")},
        )
        definition = SimpleNamespace(datasets=(SimpleNamespace(
            dataset_id="line-id",
            mode=MappingTargetMode.UPSERT,
            fields=(SimpleNamespace(
                target_field="x_ten", validate_only=False,
                value_source=ScalarValueSource.SOURCE,
            ),),
        ),))
        selection = SimpleNamespace(datasets=(SimpleNamespace(dataset_id="line-id", name="lines"),))

        message = prepared_target_precision_error(
            (row,), {"one"}, definition, selection, None,
            target_float_digits=(("x.model", "x_ten", (16, 10)),),
        )

        self.assertIn("x.model.x_ten", message)
        self.assertIn("precision (16, 10)", message)

    def test_reference_only_dataset_does_not_block_preparation(self) -> None:
        row = SimpleNamespace(
            row_id="one", dataset="lookup", target_model="x.model",
            proposed_values={"x_quantity": Decimal("0.123")},
        )
        definition = SimpleNamespace(datasets=(SimpleNamespace(
            dataset_id="lookup-id", mode=MappingTargetMode.REFERENCE,
            fields=(SimpleNamespace(
                target_field="x_quantity", validate_only=False,
                value_source=ScalarValueSource.SOURCE,
            ),),
        ),))
        selection = SimpleNamespace(datasets=(SimpleNamespace(dataset_id="lookup-id", name="lookup"),))
        schema = SimpleNamespace(models=(SimpleNamespace(
            name="x.model", fields=(SimpleNamespace(
                name="x_quantity", type="float", digits=(16, 2),
            ),),
        ),))

        self.assertIsNone(prepared_target_precision_error(
            (row,), {"one"}, definition, selection, schema,
        ))


if __name__ == "__main__":
    unittest.main()
