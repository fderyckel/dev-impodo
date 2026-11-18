"""Persist the inputs consumed by each authoritative scalar-provider execution."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import PurePath

from impodo.domain.coverage import (
    ReferenceBundle,
    ReferenceDataSet,
    ReferenceEntry,
    ReferenceValueKind,
)
from impodo.domain.mapping.contracts import (
    ReferenceLookupMapping,
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.preparation.staging_contracts import CanonicalStagingRun
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.staging.evaluator import (
    compile_browser_row_transformer,
    evaluate_browser_mapping,
)
from tests.domain.preparation.test_browser_arithmetic_formula import _fixture

WORKSPACE = "workspace:scalar-inputs"


def _stage(fields, rows, *, reference_bundle=None):
    definition, selection, _ = _fixture(width=1)
    definition = replace(
        definition,
        datasets=(replace(definition.datasets[0], fields=tuple(fields)),),
    )
    source = replace(selection.datasets[0], row_count=len(rows))
    selection = replace(selection, datasets=(source,))
    table = SourceTable(
        dataset=source.name,
        path=PurePath("unread-source.csv"),
        headers=tuple(column.source_name for column in source.columns),
        rows=tuple(rows),
        content_hash=source.source_evidence_hash,
    )
    return evaluate_browser_mapping(
        workspace_id=WORKSPACE,
        definition=definition,
        physical_selection=selection,
        effective_selection=selection,
        plan=None,
        loaded_tables={source.dataset_id: table},
        reference_bundle=reference_bundle,
    )


def _formula(target, expression, **kwargs):
    return ScalarFieldMapping(
        target_field=target,
        source_column_key="source:amount",
        value_type="integer",
        transform=ScalarTransformPolicy(formula=expression),
        **kwargs,
    )


class ScalarInputLineageTests(unittest.TestCase):
    def test_branch_specific_and_constant_proofs_survive_current_payload_roundtrip(
        self,
    ):
        fields = (
            _formula("x_branch", "value if column_3 == 1 else column_4"),
            _formula("x_constant", "'unparseable'"),
            _formula(
                "x_choice",
                "column_4",
                value_mappings=(ValueMapping("2.5", "7"),),
            ),
            ScalarFieldMapping(
                target_field="x_direct", source_column_key="source:amount"
            ),
        )
        rows = (
            SourceRow(
                2, {"id": "A", "amount": "2.5", "series": "1", "unused_0": "11.5"}
            ),
            SourceRow(
                8, {"id": "B", "amount": "3.5", "series": "2", "unused_0": "12.5"}
            ),
        )
        staged = _stage(fields, rows)
        proofs = tuple(
            row.lineage.field_input_keys for row in staged.canonical_run.rows
        )
        self.assertEqual(
            proofs,
            (
                {
                    "x_branch": ("source:amount", "source:series"),
                    "x_constant": (),
                    "x_choice": ("source:amount",),
                },
                {
                    "x_branch": ("source:series", "source:unused_0"),
                    "x_constant": (),
                    "x_choice": ("source:amount", "source:unused_0"),
                },
            ),
        )
        self.assertTrue(all("x_direct" not in proof for proof in proofs))
        # Saved declaration stays intact even when the executed branch uses another column.
        self.assertEqual(
            staged.canonical_run.rows[1].lineage.field_sources["x_branch"],
            ("source:amount",),
        )
        payload = json.loads(json.dumps(staged.canonical_run.to_portable_dict()))
        restored = CanonicalStagingRun.from_dict(payload)
        self.assertEqual(restored, staged.canonical_run)
        self.assertEqual(restored.content_hash, staged.canonical_run.content_hash)
        self.assertTrue(
            all(
                any(issue.code == "SOURCE_TYPE_INVALID" for issue in row.issues)
                for row in restored.rows
            )
        )

    def test_fallback_inspects_raw_blank_even_when_formula_is_constant(self):
        staged = _stage(
            (
                _formula(
                    "x_fallback",
                    "'unparseable'",
                    value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
                    literal_value="9",
                ),
            ),
            (
                SourceRow(
                    3, {"id": "A", "amount": "  ", "series": "1", "unused_0": "2"}
                ),
            ),
        )
        self.assertEqual(
            staged.canonical_run.rows[0].lineage.field_input_keys,
            {"x_fallback": ("source:amount",)},
        )

    def test_compiled_arithmetic_and_safe_formula_record_same_consumed_source_keys(
        self,
    ):
        definition, selection, rows = _fixture(width=1)
        field = _formula("x_result", "column_2 + column_3")
        mapping = replace(definition.datasets[0], fields=(field,))
        transformer = compile_browser_row_transformer(
            selection.datasets[0], selection.datasets[0], mapping, None, "source"
        )
        projected = transformer.project(rows[0])
        transformer.finish(projected)
        self.assertEqual(
            projected.field_input_keys, {"x_result": ("source:amount", "source:series")}
        )

    def test_reference_lookup_records_its_whole_key_on_unknown_but_none_without_package(
        self,
    ):
        reference = ReferenceDataSet(
            reference_id="11111111-1111-4111-8111-111111111111",
            version=1,
            name="Approved values",
            key_fields=("amount", "series"),
            value_kinds={"result": ReferenceValueKind.DISPLAY_VALUE},
            entries=(ReferenceEntry(key=("known", "1"), values={"result": "7"}),),
            owner="Tester",
            classification="internal",
            effective_label="Now",
        )
        field = ScalarFieldMapping(
            target_field="x_lookup",
            source_column_key="source:series",
            value_type="integer",
            reference_lookup=ReferenceLookupMapping(
                reference_id=reference.reference_id,
                reference_content_hash=reference.content_hash,
                key_source_column_keys=("source:series", "source:amount"),
                value_field="result",
            ),
        )
        rows = (
            SourceRow(
                2, {"id": "A", "amount": "missing", "series": "1", "unused_0": "2"}
            ),
        )
        for bundle, expected in (
            (None, ()),
            (
                ReferenceBundle(WORKSPACE, (reference,)),
                ("source:amount", "source:series"),
            ),
        ):
            with self.subTest(package=bundle is not None):
                staged = _stage((field,), rows, reference_bundle=bundle)
                row = staged.canonical_run.rows[0]
                self.assertEqual(row.lineage.field_input_keys, {"x_lookup": expected})
                self.assertTrue(
                    any(issue.code.startswith("REFERENCE_") for issue in row.issues)
                )
