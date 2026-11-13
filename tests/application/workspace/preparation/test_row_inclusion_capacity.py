"""Parquet row-admission qualification keeps actual routes and guards explicit."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.application.workspace.preparation import bounded_preparation as bounded
from impodo.application.workspace.preparation import (
    preparation_capability as capability,
)
from impodo.domain.mapping.contracts import RowInclusionCondition, RowInclusionPolicy
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from tests.application.workspace.preparation.test_capability import (
    _definition,
    _selection,
)


class RowInclusionCapacityTests(unittest.TestCase):
    def configure(self, count, *, formula=False):
        self.selection = _selection((count,))
        definition = _definition(self.selection)
        mapping = definition.datasets[0]
        self.definition = replace(
            definition,
            datasets=(
                replace(
                    mapping,
                    row_inclusion=RowInclusionPolicy(
                        mode="matching_rows",
                        conditions=(
                            RowInclusionCondition(
                                str(uuid4()), "column:1", "is_not_blank"
                            ),
                        ),
                    ),
                    fields=(
                        replace(
                            mapping.fields[0],
                            value_type="decimal",
                            transform=ScalarTransformPolicy(formula="value / 1000"),
                        ),
                    )
                    if formula
                    else mapping.fields,
                ),
            ),
        )
        self.snapshots = (
            SimpleNamespace(dataset_id=self.selection.datasets[0].dataset_id),
        )

    def manifest(self):
        return capability.compile_preparation_capability(
            definition=self.definition,
            physical_selection=self.selection,
            effective_selection=self.selection,
            source_snapshots=self.snapshots,
            derived_plan=None,
            current_ruleset=None,
            reference_bundle=None,
        )

    def test_verified_rows_to_use_is_bounded_python_at_100000_not_native(self):
        for count in (54_191, 100_000, 100_001):
            with self.subTest(count=count):
                self.configure(count)
                with (
                    patch.object(capability, "validate_snapshot_for_dataset"),
                    patch.object(bounded, "validate_snapshot_for_dataset"),
                ):
                    manifest = self.manifest()
                self.assertEqual(manifest.supported_rows, 100_000)
                self.assertEqual(manifest.admitted, count <= 100_000)
                transformation = next(
                    stage
                    for stage in manifest.stages
                    if stage.stage == "transformation"
                )
                self.assertEqual(
                    transformation.behavior,
                    capability.PreparationRouteBehavior.BOUNDED_PYTHON,
                )
                self.assertEqual(
                    transformation.reason_codes, ("PARQUET_ROW_INCLUSION",)
                )
                self.assertEqual(
                    manifest.datasets[0].reason_codes,
                    ("COLUMNAR_ROW_INCLUSION_UNSUPPORTED",),
                )

    def test_other_unsupported_operations_keep_their_50000_limit(self):
        self.configure(54_191, formula=True)
        with (
            patch.object(capability, "validate_snapshot_for_dataset"),
            patch.object(bounded, "validate_snapshot_for_dataset"),
        ):
            manifest = self.manifest()
        self.assertEqual(manifest.supported_rows, 50_000)
        self.assertFalse(manifest.admitted)
        self.assertIn("COLUMNAR_FORMULA_UNSUPPORTED", manifest.datasets[0].reason_codes)

    def test_missing_duplicate_or_stale_snapshots_do_not_gain_capacity(self):
        self.configure(54_191)
        original = self.snapshots
        for snapshots in ((), (*original, *original), original):
            with self.subTest(count=len(snapshots)):
                self.snapshots = snapshots
                with (
                    patch.object(
                        capability,
                        "validate_snapshot_for_dataset",
                        side_effect=SourceLoadError("stale"),
                    ),
                    patch.object(
                        bounded,
                        "validate_snapshot_for_dataset",
                        side_effect=SourceLoadError("stale"),
                    ),
                ):
                    manifest = self.manifest()
                self.assertEqual(manifest.supported_rows, 50_000)
                self.assertFalse(manifest.admitted)


if __name__ == "__main__":
    unittest.main()
