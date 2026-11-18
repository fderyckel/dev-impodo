"""Keep scoped reviewed choices as one original physical source cell."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import load_workbook

from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.data_version.source_snapshots import load_source_snapshot_table
from impodo.domain.mapping.contracts import (
    MappingDefinition,
    ReferenceKeyMapping,
    ValueMapping,
)
from impodo.domain.preflight.comparison_fields import (
    COMPARISON_RELATIONSHIP_CODES,
    comparison_relationship_findings,
)
from impodo.domain.preflight.fields_set_aside import field_set_aside_evidence
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalValueState,
)
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.shared.models import Classification, Decision, Issue
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from tests.integration.artifacts import test_prepared_original_values as prepared
from tests.integration.artifacts import test_relationship_original_values as sources
from tests.integration.artifacts import test_scoped_original_values as scoped

_SELECTED = ("DESTINATION", "ORG-003", "NORTH")


def _translated_fixture(
    directory, raw, *, selected_source=None, targets=("category_id",), blank_choices=()
):
    fixture = prepared._prepared_fixture(directory, raw, None)
    relation = fixture.mapping.relationships[0]
    resolver = replace(
        relation.resolver,
        model="x.destination",
        key_mappings=(ReferenceKeyMapping(sources._KEY, "code"),),
        scope_mappings=(
            ReferenceKeyMapping(sources._KEY, "organization"),
            ReferenceKeyMapping(sources._KEY, "division"),
        ),
        value_mappings=(
            ValueMapping(
                selected_source if selected_source is not None else raw.strip(),
                json.dumps(_SELECTED, separators=(",", ":")),
            ),
        ),
    )
    relation = replace(relation, resolver=resolver, blank_source_values=blank_choices)
    mapping = replace(
        fixture.mapping,
        relationships=tuple(
            replace(relation, target_field=target) for target in targets
        ),
    )
    definition = MappingDefinition(
        "translated_originals",
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


def _comparison(fixture, code="REFERENCE_NOT_FOUND", targets=("category_id",)):
    record = fixture.records[0]
    decision = Decision(
        record.dataset,
        record.source_row,
        record.target_identity,
        record.target_scope,
        Classification.BLOCKED,
        2 if code == "REFERENCE_AMBIGUOUS" else 0,
        source_trace_id=record.source_trace_id,
        issues=tuple(
            Issue(
                code,
                "Review the saved related-record match.",
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
            "target_match_count": decision.target_match_count,
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


class TranslatedOriginalInputExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_three_comparison_errors_keep_one_literal_choice_across_both_majors_and_modes(
        self,
    ):
        for index, raw in enumerate(("000745", "  000745  ", "=1+2")):
            fixture = _translated_fixture(self.root / f"source-{index}", raw)
            self.assertFalse(fixture.upload.exists())
            reference = fixture.rows[0].references["category_id"]
            self.assertEqual(reference.key, (_SELECTED[0],))
            self.assertEqual(reference.scope, _SELECTED[1:])
            self.assertEqual(
                fixture.rows[0].lineage.field_sources["category_id"], (sources._KEY,)
            )
            for code in sorted(COMPARISON_RELATIONSHIP_CODES):
                record, decision, fields = _comparison(fixture, code)
                planned = scoped._plan(fixture, fields=fields)
                self.assertEqual(
                    planned.original_source_requests[0].cells,
                    (
                        OriginalSourceCoordinate(
                            sources._DATASET, "partners", 2, sources._KEY
                        ),
                    ),
                )
                self.assertEqual(len(planned.original_source_requests[0].targets), 1)
                with (
                    patch.object(
                        fixture.artifacts,
                        "materialize_source",
                        side_effect=AssertionError(
                            "Translation export reopened raw source"
                        ),
                    ),
                    patch.object(
                        fixture.artifacts,
                        "materialize_source_snapshot",
                        wraps=fixture.artifacts.materialize_source_snapshot,
                    ) as materialize,
                ):
                    evidence = populate_workbook_original_values(
                        planned, fixture.artifacts
                    )
                materialize.assert_called_once()
                finding = evidence.fields_set_aside.findings[0]
                self.assertEqual(len(finding.source_inputs), 1)
                self.assertEqual(finding.source_inputs[0].value.text, raw)
                self.assertEqual(
                    finding.source_inputs[0].value.state, OriginalValueState.PRESENT
                )
                for major in (19, 20):
                    for mode in ("local", "remote"):
                        with self.subTest(raw=raw, code=code, major=major, mode=mode):
                            path = scoped._write_comparison(
                                self.root / f"{index}-{code}-{major}-{mode}",
                                evidence,
                                record,
                                decision,
                                major,
                                mode,
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
                                self.assertNotIn(
                                    json.dumps(_SELECTED), str(tuple(sheet.values))
                                )

    def test_shared_translation_outcomes_count_one_cell_and_recreate_the_same_projection(
        self,
    ):
        targets = ("category_id", "other_category_id")
        fixture = _translated_fixture(
            self.root / "source", "  000745  ", targets=targets
        )
        record, decision, fields = _comparison(fixture, targets=targets)
        planned = scoped._plan(fixture, fields=fields)
        self.assertEqual(
            planned.original_source_requests[0].cells,
            (OriginalSourceCoordinate(sources._DATASET, "partners", 2, sources._KEY),),
        )
        self.assertEqual(len(planned.original_source_requests[0].targets), 2)
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            wraps=fixture.artifacts.materialize_source_snapshot,
        ) as materialize:
            evidence = populate_workbook_original_values(planned, fixture.artifacts)
        materialize.assert_called_once()
        first = scoped._write_comparison(
            self.root / "first", evidence, record, decision
        )
        again = scoped._write_comparison(
            self.root / "again", evidence, record, decision
        )
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
                tuple(sheet.cell(row, 5).value for row in (7, 8)), ("  000745  ",) * 2
            )
            self.assertEqual(sheet["A4"].value, "1 identified source cell")
            self.assertIn("2 saved findings", sheet["G4"].value)

    def test_restricted_translation_reads_no_original_and_exports_no_source_choice(
        self,
    ):
        raw = "PRIVATE-SOURCE-CHOICE"
        fixture = _translated_fixture(self.root / "source", raw)
        record, decision, fields = _comparison(fixture)
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("Restricted translation read source"),
        ):
            evidence = populate_workbook_original_values(
                scoped._plan(fixture, fields=fields, restricted=True), fixture.artifacts
            )
        self.assertEqual(fixture.sources.calls, [])
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())
        path = scoped._write_comparison(
            self.root / "output", evidence, record, decision
        )
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
        with ZipFile(path) as archive:
            self.assertNotIn(
                raw,
                "".join(
                    archive.read(name).decode()
                    for name in archive.namelist()
                    if name.endswith(".xml")
                ),
            )

    def test_unmatched_blank_and_explicit_blank_choices_do_not_invent_findings(self):
        for index, raw in enumerate(("UNREVIEWED", None, "", "SKIP")):
            with self.subTest(raw=raw):
                fixture = _translated_fixture(
                    self.root / str(index),
                    raw,
                    selected_source="000745",
                    blank_choices=("SKIP",),
                )
                self.assertEqual(fixture.rows[0].issues, ())
                self.assertEqual(fixture.fields.findings, ())
                with patch.object(
                    fixture.artifacts,
                    "materialize_source_snapshot",
                    side_effect=AssertionError("No finding required original data"),
                ):
                    evidence = populate_workbook_original_values(
                        fixture.plan(), fixture.artifacts
                    )
                self.assertEqual(fixture.sources.calls, [])
                self.assertEqual(evidence.fields_set_aside.findings, ())
                self.assertEqual(evidence.original_source_requests, ())
                path = sources._workbook(self.root / f"output-{index}", evidence)
                with closing(load_workbook(path)) as workbook:
                    self.assertIn(
                        "0 saved findings", workbook["Fields set aside"]["G4"].value
                    )
