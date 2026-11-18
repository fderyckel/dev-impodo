"""Export only the physical cells that contributed to a prepared linked reference."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
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
    ReferenceKeyMapping,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
)
from impodo.domain.preflight.comparison_fields import comparison_relationship_findings
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideReason,
    field_set_aside_evidence,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalValueState,
)
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.shared.models import Classification, Decision, Issue
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.integration.artifacts import test_relationship_original_values as sources
from tests.integration.artifacts import test_scoped_original_values as scoped

_GENERATED = "generated:scope"


def _prepared_fixture(
    directory,
    raw,
    preparation,
    *,
    scoped_scope=False,
    scope_preparation=None,
    targets=("category_id",),
):
    fixture = sources._fixture(directory, (raw,), kind="many2one")
    relation = fixture.mapping.relationships[0]
    resolver = replace(
        relation.resolver,
        key_mappings=(ReferenceKeyMapping(sources._KEY, "name", preparation),),
        scope_mappings=(
            ReferenceKeyMapping(sources._SCOPE, "company_id", scope_preparation),
        )
        if scoped_scope
        else (),
    )
    relation = replace(
        relation,
        required=False,
        source_column_keys=(sources._KEY, sources._SCOPE)
        if scoped_scope
        else (sources._KEY,),
        resolver=resolver,
    )
    mapping = replace(
        fixture.mapping,
        target_identity=(IdentityComponentMapping((sources._ID,), ("ref",)),),
        relationships=tuple(
            replace(relation, target_field=target) for target in targets
        ),
    )
    definition = MappingDefinition(
        "prepared_originals",
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
    evaluated = evaluate_browser_mapping(
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


def _comparison(fixture, targets=("category_id",)):
    record = fixture.records[0]
    decision = Decision(
        record.dataset,
        record.source_row,
        record.target_identity,
        record.target_scope,
        Classification.BLOCKED,
        0,
        source_trace_id=record.source_trace_id,
        issues=tuple(
            Issue(
                "REFERENCE_NOT_FOUND",
                "The prepared reference did not match a related record.",
                dataset=record.dataset,
                row=record.source_row,
                field=target,
            )
            for target in targets
        ),
    )
    portable = (
        {
            "dataset": record.dataset,
            "source_row": record.source_row,
            "source_trace_id": record.source_trace_id,
            "classification": "BLOCKED",
            "issues": [
                {"code": issue.code, "field": issue.field, "message": issue.message}
                for issue in decision.issues
            ],
        },
    )
    fields = replace(
        fixture.fields,
        findings=comparison_relationship_findings(
            portable, fixture.fields, records={record.source_trace_id: record}
        ),
    )
    return record, decision, fields


def _hydrate(fixture, fields=None, **options):
    with (
        patch.object(
            fixture.artifacts,
            "materialize_source",
            side_effect=AssertionError("Export reopened the removed upload"),
        ),
        patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            wraps=fixture.artifacts.materialize_source_snapshot,
        ) as materialize,
    ):
        evidence = populate_workbook_original_values(
            scoped._plan(fixture, fields=fields, **options), fixture.artifacts
        )
    return evidence, materialize.call_count


class PreparedOriginalInputExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_formula_ignoring_anchor_and_constant_component_export_only_actual_scope_input(
        self,
    ):
        cases = (
            (
                "ignored-anchor",
                ReferencePreparation(
                    formula='concat("prefix/", column_2)',
                    bindings=(("column_2", sources._SCOPE),),
                ),
                False,
            ),
            ("fixed-key", ReferencePreparation(formula='"fixed"'), True),
        )
        for name, preparation, scoped_scope in cases:
            fixture = _prepared_fixture(
                self.root / name,
                "UNUSED-ANCHOR",
                preparation,
                scoped_scope=scoped_scope,
            )
            saved_keys = fixture.rows[0].lineage.field_sources["category_id"]
            self.assertEqual(saved_keys, (sources._KEY, sources._SCOPE))
            record, decision, fields = _comparison(fixture)
            planned = scoped._plan(fixture, fields=fields)
            self.assertEqual(
                planned.fields_set_aside.findings[0].source_keys, saved_keys
            )
            self.assertEqual(
                tuple(
                    item.coordinate.source_key
                    for item in planned.fields_set_aside.findings[0].source_inputs
                ),
                (sources._SCOPE,),
            )
            self.assertEqual(
                planned.original_source_requests[0].cells,
                (
                    OriginalSourceCoordinate(
                        sources._DATASET, "partners", 2, sources._SCOPE
                    ),
                ),
            )
            evidence, reads = _hydrate(fixture, fields)
            self.assertEqual(reads, 1)
            self.assertFalse(fixture.upload.exists())
            for major in (19, 20):
                for mode in ("local", "remote"):
                    with self.subTest(name=name, major=major, mode=mode):
                        path = scoped._write_comparison(
                            self.root / f"{name}-{major}-{mode}",
                            evidence,
                            record,
                            decision,
                            major,
                            mode,
                        )
                        with closing(load_workbook(path)) as workbook:
                            sheet = workbook["Fields set aside"]
                            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 1)
                            self.assertEqual(sheet["D7"].value, "Scope")
                            self.assertEqual(sheet["E7"].value, "company")
                            self.assertEqual(
                                sheet["A4"].value, "1 identified source cell"
                            )
                            self.assertIn("1 saved finding", sheet["G4"].value)
                            self.assertNotIn("UNUSED-ANCHOR", str(tuple(sheet.values)))

    def test_parts_literals_and_repeated_inputs_are_physical_cells_once_per_outcome(
        self,
    ):
        preparation = ReferencePreparation(
            parts=(
                ReferencePart(source_column_key=sources._SCOPE),
                ReferencePart(literal="/Fixed/"),
                ReferencePart(source_column_key=sources._KEY),
                ReferencePart(literal="/"),
                ReferencePart(source_column_key=sources._SCOPE),
            )
        )
        scope_preparation = ReferencePreparation(
            formula='concat("scope/", column_1)', bindings=(("column_1", sources._ID),)
        )
        targets = ("category_id", "other_category_id")
        fixture = _prepared_fixture(
            self.root / "source",
            "000745",
            preparation,
            scoped_scope=True,
            scope_preparation=scope_preparation,
            targets=targets,
        )
        self.assertEqual(
            fixture.rows[0].references["category_id"].key,
            ("company/Fixed/000745/company",),
        )
        record, decision, fields = _comparison(fixture, targets)
        evidence, reads = _hydrate(fixture, fields)
        self.assertEqual(reads, 1)
        self.assertEqual(len(evidence.fields_set_aside.findings), 2)
        for finding in evidence.fields_set_aside.findings:
            self.assertEqual(
                finding.source_keys, (sources._KEY, sources._SCOPE, sources._ID)
            )
            self.assertEqual(
                tuple(item.value.text for item in finding.source_inputs),
                ("000745", "company", "row-1"),
            )
        path = scoped._write_comparison(
            self.root / "output", evidence, record, decision
        )
        recreated = scoped._write_comparison(
            self.root / "again", evidence, record, decision
        )
        with (
            closing(load_workbook(path)) as workbook,
            closing(load_workbook(recreated)) as again,
        ):
            sheet = workbook["Fields set aside"]
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 6)
            self.assertEqual(sheet["A4"].value, "3 identified source cells")
            self.assertIn("2 saved findings", sheet["G4"].value)
            self.assertEqual(
                tuple(sheet.values), tuple(again["Fields set aside"].values)
            )
            self.assertNotIn(
                "/Fixed/", tuple(sheet.cell(row, 5).value for row in range(7, 13))
            )

    def test_optional_failed_preparation_retains_blank_original_and_invalid_reason(
        self,
    ):
        preparation = ReferencePreparation(
            parts=(
                ReferencePart(source_column_key=sources._KEY),
                ReferencePart(literal="/"),
                ReferencePart(source_column_key=sources._SCOPE),
            )
        )
        fixture = _prepared_fixture(self.root / "source", "", preparation)
        self.assertFalse(fixture.mapping.relationships[0].required)
        self.assertIsNone(fixture.rows[0].references["category_id"])
        self.assertEqual(
            {issue.code for issue in fixture.rows[0].issues},
            {"REFERENCE_PREPARATION_INVALID"},
        )
        self.assertEqual(len(fixture.fields.findings), 1)
        self.assertEqual(fixture.fields.findings[0].reason, FieldSetAsideReason.INVALID)
        evidence, reads = _hydrate(fixture)
        self.assertEqual(reads, 1)
        inputs = evidence.fields_set_aside.findings[0].source_inputs
        self.assertEqual(tuple(item.value.text for item in inputs), ("", "company"))
        self.assertEqual(inputs[0].value.state, OriginalValueState.BLANK)
        path = sources._workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["A7"].value, FieldSetAsideReason.INVALID.value)
            self.assertIsNone(sheet["E7"].value)
            self.assertIn("empty text", sheet["E7"].comment.text)
            self.assertEqual(sheet["E8"].value, "company")
            self.assertIn("1 saved finding", sheet["G4"].value)

    def test_invalid_and_constant_only_rules_request_no_original_sources(self):
        for name, preparation in (
            ("invalid", ReferencePreparation(formula="value +")),
            ("constant", ReferencePreparation(formula='"fixed"')),
        ):
            with self.subTest(name=name):
                fixture = _prepared_fixture(self.root / name, "000745", preparation)
                fields = fixture.fields
                if not fields.findings:
                    _, _, fields = _comparison(fixture)
                with patch.object(
                    fixture.artifacts,
                    "materialize_source_snapshot",
                    side_effect=AssertionError("Unprovable reference read originals"),
                ):
                    evidence = populate_workbook_original_values(
                        scoped._plan(fixture, fields=fields), fixture.artifacts
                    )
                self.assertEqual(fixture.sources.calls, [])
                self.assertEqual(evidence.original_source_requests, ())
                self.assertEqual(
                    evidence.fields_set_aside.findings[0].source_inputs, ()
                )

    def test_generated_preparation_dependency_rejects_whole_group(self):
        preparation = ReferencePreparation(
            formula='concat(value, "/", column_2)',
            bindings=(("column_2", sources._SCOPE),),
        )
        fixture = _prepared_fixture(self.root / "source", "000745", preparation)
        _, _, fields = _comparison(fixture)
        physical = fixture.selection.datasets[0]
        effective = replace(
            physical,
            columns=(
                *physical.columns,
                SourceDatasetColumn(4, "Generated input", _GENERATED, "string"),
            ),
        )
        selection = replace(
            fixture.selection, datasets=(effective,), content_hash="sha256:" + "b" * 64
        )
        relation = fixture.mapping.relationships[0]
        prepared_key = replace(
            relation.resolver.key_mappings[0],
            preparation=replace(preparation, bindings=(("column_2", _GENERATED),)),
        )
        mapping = replace(
            fixture.mapping,
            relationships=(
                replace(
                    relation,
                    resolver=replace(relation.resolver, key_mappings=(prepared_key,)),
                ),
            ),
        )
        finding = replace(fields.findings[0], source_keys=(sources._KEY, _GENERATED))
        row = fixture.rows[0]
        row = replace(
            row,
            lineage=replace(
                row.lineage,
                source_selection_hash=selection.content_hash,
                field_sources={
                    **row.lineage.field_sources,
                    "category_id": finding.source_keys,
                },
            ),
        )
        fixture = replace(fixture, rows=(row,))
        fields = replace(
            fields, findings=(replace(finding, source_trace_id=row.row_id),)
        )
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("Partial physical group was read"),
        ):
            evidence = populate_workbook_original_values(
                scoped._plan(
                    fixture, fields=fields, mapping=mapping, selection=selection
                ),
                fixture.artifacts,
            )
        self.assertEqual(fixture.sources.calls, ["selection"])
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())

    def test_restricted_prepared_finding_has_no_source_reads_or_geometry_expansion(
        self,
    ):
        preparation = ReferencePreparation(
            parts=(
                ReferencePart(source_column_key=sources._KEY),
                ReferencePart(literal="/"),
                ReferencePart(source_column_key=sources._SCOPE),
            )
        )
        fixture = _prepared_fixture(self.root / "source", "", preparation)
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("Restricted originals read"),
        ):
            evidence = populate_workbook_original_values(
                scoped._plan(fixture, restricted=True), fixture.artifacts
            )
        self.assertEqual(fixture.sources.calls, [])
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())
        path = sources._workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 1)
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
