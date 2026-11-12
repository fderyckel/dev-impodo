"""Trace aliased columns through multiple saved joins without evaluating rows."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase

from impodo.domain.preflight.attention_lineage import attention_column_origins
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.derived_entities import mapping_source_selection
from tests.domain.recipe import test_bom_source_composition as bom


class AttentionColumnOriginTests(TestCase):
    def test_renamed_columns_trace_through_chained_joins_to_original_headings(self):
        fixture = bom.BomSourceCompositionQualificationTests(methodName="runTest")
        fixture.setUp()
        header = structural_dataset_id(fixture.header_rule)
        lines = structural_dataset_id(fixture.line_rule)
        combined = ExactJoinRule(
            rule_id="00000000-0000-0000-0000-000000000103",
            output_dataset_name="lines_with_headers",
            left_dataset_id=header,
            right_dataset_id=lines,
            keys=(
                JoinKey("header:company", "line:company"),
                JoinKey("header:bom", "line:bom"),
            ),
            output_columns=(
                StructuralOutputColumn(
                    "joined:description", "Header description", "string"
                ),
                StructuralOutputColumn("joined:scrap", "Scrap", "decimal"),
            ),
            projections=(
                StructuralProjection(
                    "joined:description", header, "header:description"
                ),
                StructuralProjection("joined:scrap", lines, "line:scrap"),
            ),
        )
        plan = replace(fixture.plan, rules=(*fixture.plan.rules, combined))
        selection = mapping_source_selection(fixture.selection, plan)
        # This pure projection needs only the already-verified snapshot headers.
        snapshots = tuple(
            SimpleNamespace(
                dataset_id=dataset.dataset_id,
                schema=SimpleNamespace(columns=dataset.columns),
            )
            for dataset in fixture.selection.datasets
        )
        origins = attention_column_origins(selection, snapshots, plan)
        self.assertEqual(
            origins["lines_with_headers"],
            {
                "joined:description": ("header-detail:description",),
                "joined:scrap": ("line-detail:scrap",),
            },
        )
