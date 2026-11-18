"""Recover one scalar formula input from saved failures without recalculating it."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.data_version.source_snapshots import load_source_snapshot_table
from impodo.domain.mapping.contracts import (
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideReason,
    field_set_aside_evidence,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalValueState,
)
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.recipe import value_rules
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.staging import evaluator
from tests.integration.artifacts import test_relationship_original_values as sources
from tests.integration.artifacts import test_scoped_original_values as scoped


def _scalar_fixture(
    directory,
    raw,
    formula,
    *,
    provider=ScalarValueSource.SOURCE,
    fallback=None,
    targets=("x_amount",),
):
    fixture = sources._fixture(directory, (raw,), kind="many2one")
    mapping = replace(
        fixture.mapping,
        target_model="x.measure",
        target_identity=(IdentityComponentMapping((sources._ID,), ("x_code",)),),
        fields=tuple(
            ScalarFieldMapping(
                target,
                sources._KEY,
                value_source=provider,
                literal_value=fallback,
                value_type="decimal",
                transform=ScalarTransformPolicy(formula=formula),
            )
            for target in targets
        ),
        relationships=(),
    )
    definition = MappingDefinition(
        "scalar_originals",
        fixture.selection.content_hash,
        "sha256:" + "b" * 64,
        (mapping,),
    )
    snapshot = fixture.sources.snapshot
    with fixture.artifacts.materialize_source_snapshot(
        snapshot.data_version_id,
        snapshot.parquet_storage_key,
        expected_sha256=snapshot.parquet_sha256,
    ) as path:
        table = load_source_snapshot_table(path, snapshot)
    evaluated = evaluator.evaluate_browser_mapping(
        workspace_id="workspace-1",
        definition=definition,
        physical_selection=fixture.selection,
        effective_selection=fixture.selection,
        plan=None,
        loaded_tables={mapping.dataset_id: table},
    )
    rows = tuple(
        CanonicalRow.from_dict(row.to_portable_dict())
        for row in evaluated.canonical_run.rows
    )
    lineage = {
        (row.dataset, target): keys
        for row in rows
        for target, keys in row.lineage.field_sources.items()
    }
    fields = field_set_aside_evidence(
        evaluated.plan,
        rows,
        evaluated.source_field_labels,
        lineage,
        column_exclusions={},
    )
    records = tuple(
        replace(record, source_trace_id=row.row_id)
        for record, row in zip(evaluated.prepared.records, rows, strict=True)
    )
    return replace(fixture, mapping=mapping, fields=fields, rows=rows, records=records)


@contextmanager
def _no_runtime_evaluation():
    with ExitStack() as stack:
        for owner, name in (
            (value_rules, "evaluate_formula"),
            (value_rules.CompiledArithmeticFormula, "evaluate"),
            (evaluator, "evaluate_scalar_mapping_value"),
        ):
            stack.enter_context(
                patch.object(
                    owner,
                    name,
                    side_effect=AssertionError(
                        "Workbook export recalculated a formula"
                    ),
                )
            )
        yield


class ScalarOriginalInputExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_saved_formula_and_conversion_failures_keep_the_literal_single_input(self):
        cases = (
            ("000745", "value / 0", "SOURCE_FORMULA_INVALID"),
            ("  000745  ", "value / (column_3 - column_3)", "SOURCE_FORMULA_INVALID"),
            ("  =literal  ", "upper(column_3)", "SOURCE_TYPE_INVALID"),
        )
        for index, (raw, formula, code) in enumerate(cases):
            fixture = _scalar_fixture(self.root / f"source-{index}", raw, formula)
            self.assertFalse(fixture.upload.exists())
            self.assertEqual({issue.code for issue in fixture.rows[0].issues}, {code})
            self.assertEqual(
                fixture.rows[0].lineage.field_sources["x_amount"], (sources._KEY,)
            )
            self.assertEqual(len(fixture.fields.findings), 1)
            self.assertEqual(
                fixture.fields.findings[0].reason, FieldSetAsideReason.INVALID
            )
            with (
                _no_runtime_evaluation(),
                patch.object(
                    fixture.artifacts,
                    "materialize_source",
                    side_effect=AssertionError("Removed upload reopened"),
                ),
                patch.object(
                    fixture.artifacts,
                    "materialize_source_snapshot",
                    wraps=fixture.artifacts.materialize_source_snapshot,
                ) as materialize,
            ):
                planned = scoped._plan(fixture)
                self.assertEqual(
                    planned.original_source_requests[0].cells,
                    (
                        OriginalSourceCoordinate(
                            sources._DATASET, "partners", 2, sources._KEY
                        ),
                    ),
                )
                evidence = populate_workbook_original_values(planned, fixture.artifacts)
                for major in (19, 20):
                    for mode in ("local", "remote"):
                        with self.subTest(raw=raw, major=major, mode=mode):
                            path = sources._workbook(
                                self.root / f"{index}-{major}-{mode}",
                                evidence,
                                major=major,
                                mode=mode,
                            )
                            with closing(load_workbook(path)) as workbook:
                                sheet = workbook["Fields set aside"]
                                self.assertEqual(
                                    len(tuple(sheet.iter_rows(min_row=7))), 1
                                )
                                self.assertEqual(sheet["D7"].value, "Relationship")
                                self.assertEqual(sheet["E7"].value, raw)
                                self.assertEqual(sheet["E7"].data_type, "s")
                                self.assertEqual(
                                    sheet["A4"].value, "1 identified source cell"
                                )
                                self.assertIn("1 saved finding", sheet["G4"].value)
            materialize.assert_called_once()
            self.assertEqual(fixture.sources.calls, ["selection", "snapshots"])

    def test_fallback_formula_failure_preserves_original_null_and_empty_text(self):
        for raw, note in ((None, "blank cell"), ("", "empty text")):
            with self.subTest(raw=raw):
                fixture = _scalar_fixture(
                    self.root / note,
                    raw,
                    "value / 0",
                    provider=ScalarValueSource.SOURCE_WITH_FALLBACK,
                    fallback="5",
                )
                self.assertEqual(
                    {issue.code for issue in fixture.rows[0].issues},
                    {"SOURCE_FORMULA_INVALID"},
                )
                with _no_runtime_evaluation():
                    evidence = populate_workbook_original_values(
                        scoped._plan(fixture), fixture.artifacts
                    )
                    value = evidence.fields_set_aside.findings[0].source_inputs[0].value
                    self.assertEqual(value.state, OriginalValueState.BLANK)
                    self.assertEqual(value.text, raw)
                    path = sources._workbook(self.root / f"{note}-output", evidence)
                with closing(load_workbook(path)) as workbook:
                    sheet = workbook["Fields set aside"]
                    self.assertIsNone(sheet["E7"].value)
                    self.assertIn(note, sheet["E7"].comment.text)
                    self.assertEqual(sheet["A4"].value, "1 identified source cell")
                    self.assertIn("1 saved finding", sheet["G4"].value)
                    self.assertNotIn(
                        "5",
                        tuple(
                            sheet.cell(row, 5).value
                            for row in range(7, sheet.max_row + 1)
                        ),
                    )

    def test_two_scalar_outcomes_share_one_cell_and_recreate_without_evaluation(self):
        fixture = _scalar_fixture(
            self.root / "source",
            "00123",
            "value / 0",
            targets=("x_amount", "x_other_amount"),
        )
        self.assertEqual(len(fixture.fields.findings), 2)
        with (
            _no_runtime_evaluation(),
            patch.object(
                fixture.artifacts,
                "materialize_source_snapshot",
                wraps=fixture.artifacts.materialize_source_snapshot,
            ) as materialize,
        ):
            planned = scoped._plan(fixture)
            self.assertEqual(
                planned.original_source_requests[0].cells,
                (
                    OriginalSourceCoordinate(
                        sources._DATASET, "partners", 2, sources._KEY
                    ),
                ),
            )
            self.assertEqual(len(planned.original_source_requests[0].targets), 2)
            evidence = populate_workbook_original_values(planned, fixture.artifacts)
            first = sources._workbook(self.root / "first", evidence)
            again = sources._workbook(self.root / "again", evidence)
        materialize.assert_called_once()
        with (
            closing(load_workbook(first)) as workbook,
            closing(load_workbook(again)) as recreated,
        ):
            sheet = workbook["Fields set aside"]
            self.assertEqual(
                tuple(sheet.values), tuple(recreated["Fields set aside"].values)
            )
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 2)
            self.assertEqual(
                tuple(sheet.cell(row, 5).value for row in (7, 8)), ("00123", "00123")
            )
            self.assertEqual(sheet["A4"].value, "1 identified source cell")
            self.assertIn("2 saved findings", sheet["G4"].value)

    def test_constant_invalid_and_untaken_formula_inputs_do_not_read_originals(self):
        for index, formula in enumerate(
            (
                "'not numeric'",
                "value +",
                "column_999 / 0",
                "value if False else 1 / 0",
                "1 > 2 > value",
            )
        ):
            with self.subTest(formula=formula):
                fixture = _scalar_fixture(self.root / str(index), "000745", formula)
                self.assertTrue(fixture.fields.findings)
                with (
                    _no_runtime_evaluation(),
                    patch.object(
                        fixture.artifacts,
                        "materialize_source_snapshot",
                        side_effect=AssertionError("Unproven formula read originals"),
                    ),
                ):
                    evidence = populate_workbook_original_values(
                        scoped._plan(fixture), fixture.artifacts
                    )
                self.assertEqual(fixture.sources.calls, [])
                self.assertEqual(evidence.original_source_requests, ())
                self.assertEqual(
                    evidence.fields_set_aside.findings[0].source_inputs, ()
                )

    def test_saved_formula_reads_preserve_other_columns_and_eager_boolean_inputs(self):
        for index, (formula, keys, expected) in enumerate(
            (
                (
                    "value + column_2",
                    (sources._SCOPE, sources._KEY),
                    ("company", "000745"),
                ),
                ("True or value", (sources._KEY,), ("000745",)),
            )
        ):
            with self.subTest(formula=formula):
                fixture = _scalar_fixture(
                    self.root / f"readset-{index}", "000745", formula
                )
                self.assertEqual(
                    fixture.rows[0].lineage.field_input_keys["x_amount"], keys
                )
                with _no_runtime_evaluation():
                    evidence = populate_workbook_original_values(
                        scoped._plan(fixture), fixture.artifacts
                    )
                inputs = evidence.fields_set_aside.findings[0].source_inputs
                self.assertEqual(
                    tuple(item.coordinate.source_key for item in inputs), keys
                )
                self.assertEqual(tuple(item.value.text for item in inputs), expected)
                self.assertEqual(fixture.sources.calls, ["selection", "snapshots"])

    def test_restricted_formula_failure_does_not_read_or_expose_original(self):
        fixture = _scalar_fixture(self.root / "source", "  =private  ", "upper(value)")
        with (
            _no_runtime_evaluation(),
            patch.object(
                fixture.artifacts,
                "materialize_source_snapshot",
                side_effect=AssertionError("Restricted formula read originals"),
            ),
        ):
            evidence = populate_workbook_original_values(
                scoped._plan(fixture, restricted=True), fixture.artifacts
            )
            path = sources._workbook(self.root / "output", evidence)
        self.assertEqual(fixture.sources.calls, [])
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertNotIn("=private", str(tuple(sheet.values)))
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
