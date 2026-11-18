"""Plan original cells for single-anchor scalar formulas with bounded source reads."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from unittest.mock import patch

from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceValue,
)
from impodo.domain.preflight.scalar_inputs import scalar_formula_aliases
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import EncodedSourceCell, SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from tests.support.original_coordinates import (
    source_coordinate as _coord,
)
from tests.support.original_coordinates import (
    source_coordinates as _cells,
)
from tests.support.original_coordinates import (
    source_target as _target,
)

_HASH = "sha256:" + "a" * 64
_ANCHOR = "column:1:" + "1" * 12
_OTHER = "column:2:" + "2" * 12
_NOW = datetime(2026, 11, 18, tzinfo=UTC)


def _selection() -> SourceSelection:
    return SourceSelection(
        "selection-1",
        1,
        "data-version-1",
        _NOW,
        "Data manager",
        (
            SourceDataset(
                "dataset:" + "1" * 24,
                "Customers",
                FileSourceBinding(
                    "file-1", "Customers.csv", _HASH, _HASH, "utf-8", ",", 1
                ),
                2,
                (
                    SourceDatasetColumn(1, "Anchor", _ANCHOR, "string"),
                    SourceDatasetColumn(2, "Other", _OTHER, "string"),
                ),
            ),
        ),
        _HASH,
    )


def _field(formula: str = "value", *, fallback: bool = False) -> ScalarFieldMapping:
    return ScalarFieldMapping(
        "name",
        _ANCHOR,
        value_source=ScalarValueSource.SOURCE_WITH_FALLBACK
        if fallback
        else ScalarValueSource.SOURCE,
        literal_value="Fallback" if fallback else None,
        transform=ScalarTransformPolicy(formula=formula),
    )


def _finding(
    *,
    target: str = "name",
    keys: tuple[str, ...] = (_ANCHOR,),
    comparison: bool = False,
) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.INVALID,
        "Customers",
        19,
        keys,
        target,
        "Saved scalar rule failure.",
        comparison_issue=comparison,
    )


def _fields(*findings: FieldSetAside) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        tuple(findings), {}, {}, {}, frozenset(), {"Customers": "res.partner"}, ()
    )


class _Sources:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection = selection
        self.calls: list[str] = []
        dataset = selection.datasets[0]
        self.snapshots = (
            SourceSnapshot.create(
                data_version_id=selection.data_version_id,
                dataset_id=dataset.dataset_id,
                dataset_name=dataset.name,
                source=dataset.source,
                physical_selection_hash=selection.content_hash,
                schema=source_snapshot_schema(dataset),
                row_count=dataset.row_count,
                data_logical_hash=_HASH,
                parquet_sha256=_HASH,
                created_at=_NOW,
            ),
        )

    def get_mapping_source_selection(self, workspace_id: str) -> SourceSelection | None:
        raise AssertionError("Frozen selection must not be reloaded")

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append("snapshots")
        return self.snapshots


class ScalarOriginalValuePlanTests(TestCase):
    def setUp(self) -> None:
        self.selection = _selection()
        self.sources = _Sources(self.selection)
        self.mapping = DatasetMapping(
            self.selection.datasets[0].dataset_id, "res.partner", fields=(_field(),)
        )

    def plan(self, fields: FieldSetAsideEvidence, **changes):
        options = {
            "fields": fields,
            "selection": self.selection,
            "physical_selection_hash": _HASH,
            "mappings": (self.mapping,),
            "sources": self.sources,
            "workspace_id": "workspace-1",
            "restricted": False,
        }
        options.update(changes)
        return plan_workbook_original_values(**options)

    def test_saved_single_anchor_formula_retains_exact_canonical_lineage_and_target(
        self,
    ) -> None:
        fields, requests = self.plan(_fields(_finding()))
        self.assertEqual(fields.findings[0].source_keys, (_ANCHOR,))
        self.assertEqual(
            tuple(
                item.coordinate.source_key for item in fields.findings[0].source_inputs
            ),
            (_ANCHOR,),
        )
        self.assertEqual(
            requests[0].targets,
            (_target(19, _ANCHOR, "name", (_ANCHOR,)),),
        )
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_fallback_and_same_anchor_ordinal_names_still_authorize_raw_source(
        self,
    ) -> None:
        for formula in ("value", "column_1", "value + column_1"):
            with self.subTest(formula=formula):
                _, requests = self.plan(
                    _fields(_finding()),
                    mappings=(
                        replace(self.mapping, fields=(_field(formula, fallback=True),)),
                    ),
                )
                self.assertEqual(requests[0].cells, _cells(((19, _ANCHOR),)))

    def test_constant_multi_input_unknown_and_invalid_formulas_return_before_reads(
        self,
    ) -> None:
        for formula in (
            '"Fixed"',
            "column_2",
            "value + column_2",
            "column_999",
            "value +",
            "upper",
        ):
            with self.subTest(formula=formula):
                fields, requests = self.plan(
                    _fields(_finding()),
                    mappings=(
                        replace(self.mapping, fields=(_field(formula, fallback=True),)),
                    ),
                )
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_branching_formulas_have_no_proven_input_or_repository_reads(self) -> None:
        for formula in (
            'value if False else "FIXED"',
            "True or value",
            "0 > 1 > value",
            'concat("prefix", value if False else "FIXED")',
        ):
            with self.subTest(formula=formula):
                fields, requests = self.plan(
                    _fields(_finding()),
                    mappings=(replace(self.mapping, fields=(_field(formula),)),),
                )
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_formula_with_inline_choices_does_not_use_formula_only_dependency_proof(
        self,
    ) -> None:
        field = replace(_field(), value_mappings=(ValueMapping("A", "Matched"),))
        fields, requests = self.plan(
            _fields(_finding()), mappings=(replace(self.mapping, fields=(field,)),)
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_ambiguous_frozen_ordinals_are_unavailable_without_source_reads(
        self,
    ) -> None:
        dataset = self.selection.datasets[0]
        effective = replace(
            self.selection,
            datasets=(
                replace(
                    dataset,
                    columns=(
                        dataset.columns[0],
                        replace(dataset.columns[1], ordinal=1),
                    ),
                ),
            ),
        )
        fields, requests = self.plan(_fields(_finding()), selection=effective)
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_direct_no_formula_stale_anchor_still_fails_before_repository_reads(
        self,
    ) -> None:
        field = replace(_field(""), source_column_key="missing-anchor")
        with self.assertRaises(ReadinessError):
            self.plan(
                _fields(_finding(keys=("missing-anchor",))),
                mappings=(replace(self.mapping, fields=(field,)),),
            )
        self.assertEqual(self.sources.calls, [])

    def test_comparison_scalar_outcomes_remain_outside_original_capture(self) -> None:
        _, requests = self.plan(_fields(_finding(comparison=True)))
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_full_saved_lineage_must_match_the_single_anchor(self) -> None:
        for keys in ((_OTHER,), (_ANCHOR, _OTHER)):
            with self.subTest(keys=keys):
                fields, requests = self.plan(_fields(_finding(keys=keys)))
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_alias_projection_is_built_once_per_dataset_and_reads_stay_bulk(
        self,
    ) -> None:
        mapping = replace(
            self.mapping,
            fields=(_field(), replace(_field("column_1"), target_field="description")),
        )
        findings = _fields(
            *(
                _finding(target="name" if index % 2 else "description")
                for index in range(200)
            )
        )
        with patch(
            "impodo.application.workspace.review.original_values.scalar_formula_aliases",
            wraps=scalar_formula_aliases,
        ) as aliases:
            fields, requests = self.plan(findings, mappings=(mapping,))
        aliases.assert_called_once_with(self.selection.datasets[0].columns)
        self.assertEqual(len(fields.findings), 200)
        self.assertEqual(len(requests[0].cells), 1)
        self.assertEqual(len(requests[0].targets), 2)
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_restricted_values_are_cleared_before_formula_metadata_or_sources_are_read(
        self,
    ) -> None:
        finding = replace(
            _finding(),
            source_inputs=(
                OriginalSourceInput(
                    _coord(19, _ANCHOR),
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Private")
                    ),
                ),
            ),
        )
        with patch(
            "impodo.application.workspace.review.original_values.scalar_formula_aliases",
            side_effect=AssertionError("restricted metadata"),
        ):
            fields, requests = self.plan(_fields(finding), restricted=True)
        self.assertTrue(fields.source_values_restricted)
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])
