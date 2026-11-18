"""Keep scoped lookup outcomes intact while exporting each proven physical input."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.mapping.contracts import ReferenceKeyMapping, ScalarFieldMapping
from impodo.domain.preflight.comparison_fields import (
    ComparisonFieldCoverage,
    comparison_relationship_findings,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAside, FieldSetAsideReason
from impodo.domain.preflight.original_values import (
    OriginalSourceValue,
    OriginalValueState,
)
from impodo.domain.preflight.reports import ReviewWorkbookEvidence
from impodo.domain.shared.models import (
    Classification,
    Decision,
    Issue,
    PreflightResult,
    TargetFingerprint,
    canonical_json_bytes,
)
from impodo.domain.workspace.contracts import SourceDatasetColumn
from tests.integration.artifacts import test_relationship_original_values as fixtures

_SYNTHETIC_SCOPE = "generated:scope"


class _Sources:
    def __init__(self, selection, snapshots):
        self.selection = selection
        self.snapshots = snapshots
        self.calls = []

    def get_mapping_source_selection(self, workspace_id):
        raise AssertionError("The frozen effective selection must be reused")

    def get_source_selection(self, workspace_id):
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(self, workspace_id):
        self.calls.append("snapshots")
        return self.snapshots


def _plan(fixture, *, selection=None, mapping=None, fields=None, restricted=False):
    planned, requests = plan_workbook_original_values(
        fields=fields if fields is not None else fixture.fields,
        selection=selection if selection is not None else fixture.selection,
        physical_selection_hash=fixture.selection.content_hash,
        mappings=(mapping if mapping is not None else fixture.mapping,),
        sources=fixture.sources,
        workspace_id="workspace-1",
        restricted=restricted,
        workbook_lineage={row.row_id: row.lineage for row in fixture.rows},
    )
    return ReviewWorkbookEvidence(
        frozen_input_hash=fixtures._HASH,
        records=(),
        dataset_labels={"partners": "Partners"},
        target_model_labels={},
        target_field_labels={},
        normalization_content_hash="",
        cell_effects=(),
        target_field_required={},
        fields_set_aside=planned,
        original_source_requests=requests,
    )


def _comparison(fixture, targets=("category_id",)):
    record = fixture.records[0]
    references = dict(record.references)
    for target in targets:
        references[target] = references["category_id"]
    record = replace(record, references=references)
    decision = Decision(
        dataset=record.dataset,
        source_row=record.source_row,
        business_identity=record.target_identity,
        business_scope=record.target_scope,
        classification=Classification.BLOCKED,
        target_match_count=0,
        source_trace_id=record.source_trace_id,
        issues=tuple(
            Issue(
                "REFERENCE_NOT_FOUND",
                "No related record matched the complete key and scope.",
                dataset=record.dataset,
                row=record.source_row,
                field=target,
            )
            for target in targets
        ),
    )
    fields = replace(
        fixture.fields,
        source_keys={
            **fixture.fields.source_keys,
            **{
                ("partners", target): (fixtures._KEY, fixtures._SCOPE)
                for target in targets
            },
        },
    )
    findings = comparison_relationship_findings(
        (
            {
                "dataset": decision.dataset,
                "source_row": decision.source_row,
                "source_trace_id": decision.source_trace_id,
                "classification": "BLOCKED",
                "issues": [
                    {"code": issue.code, "field": issue.field, "message": issue.message}
                    for issue in decision.issues
                ],
            },
        ),
        fields,
        records={record.source_trace_id: record},
    )
    return record, decision, replace(fields, findings=findings)


def _write_comparison(directory, evidence, record, decision, major=20, mode="local"):
    directory.mkdir(parents=True, exist_ok=True)
    result = PreflightResult(
        profile_id="scoped_originals",
        source_hashes={"partners": fixtures._HASH},
        fingerprint=TargetFingerprint(
            fixtures._HASH, mode, "test", f"{major}.0", fixtures._NOW.isoformat()
        ),
        metadata_snapshot_hash=None,
        record_snapshot_hash=None,
        decisions=(decision,),
        reference_resolutions=(),
        issues=(),
    )
    payload = result.to_portable_dict()
    payload["preflight_evidence"] = {"frozen_input_hash": fixtures._HASH}
    content = canonical_json_bytes(payload)
    manifest = directory / "manifest.json"
    manifest.write_bytes(content)
    evidence = replace(
        evidence,
        records=(record,),
        comparison_fields=ComparisonFieldCoverage(
            "sha256:" + sha256(content).hexdigest(),
            result.semantic_hash,
        ),
    )
    return write_review_workbook(
        manifest, directory / "review.xlsx", review_evidence=evidence
    )


class ScopedOriginalInputExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_saved_scoped_preparation_blanks_expand_cells_without_multiplying_findings(
        self,
    ):
        for raw, note in ((None, "blank cell"), ("", "empty text")):
            with self.subTest(raw=raw):
                fixture = fixtures._fixture(
                    self.root / note, (raw,), kind="many2one", shape="scoped"
                )
                self.assertFalse(fixture.upload.exists())
                self.assertEqual(len(fixture.fields.findings), 1)
                self.assertEqual(
                    fixture.fields.findings[0].source_keys,
                    (fixtures._KEY, fixtures._SCOPE),
                )
                with (
                    patch.object(
                        fixture.artifacts,
                        "materialize_source",
                        side_effect=AssertionError("upload reopened"),
                    ),
                    patch.object(
                        fixture.artifacts,
                        "materialize_source_snapshot",
                        wraps=fixture.artifacts.materialize_source_snapshot,
                    ) as materialize,
                ):
                    evidence = populate_workbook_original_values(
                        fixture.plan(), fixture.artifacts
                    )
                materialize.assert_called_once()
                inputs = evidence.fields_set_aside.findings[0].source_inputs
                self.assertEqual(
                    tuple(item.coordinate.source_key for item in inputs),
                    (fixtures._KEY, fixtures._SCOPE),
                )
                self.assertEqual(
                    tuple(item.value.text for item in inputs), (raw, "company")
                )
                self.assertEqual(len(evidence.fields_set_aside.findings), 1)
                path = fixtures._workbook(self.root / f"{note}-output", evidence)
                with closing(load_workbook(path)) as workbook:
                    sheet = workbook["Fields set aside"]
                    rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(
                        tuple(row[3] for row in rows), ("Relationship", "Scope")
                    )
                    self.assertEqual(tuple(row[4] for row in rows), (None, "company"))
                    self.assertEqual(
                        tuple(row[6] for row in rows), ("Source cell", "Source cell")
                    )
                    self.assertEqual(sheet["A4"].value, "2 identified source cells")
                    self.assertIn("1 saved finding", sheet["G4"].value)
                    self.assertIn(note, sheet["E7"].comment.text)

    def test_saved_comparison_preserves_separate_literal_key_and_scope_for_both_majors_and_modes(
        self,
    ):
        for index, raw in enumerate(("000745", "  =1+2  ")):
            fixture = fixtures._fixture(
                self.root / f"source-{index}", (raw,), kind="many2one", shape="scoped"
            )
            record, decision, fields = _comparison(fixture)
            evidence = _plan(fixture, fields=fields)
            with patch.object(
                fixture.artifacts,
                "materialize_source_snapshot",
                wraps=fixture.artifacts.materialize_source_snapshot,
            ) as materialize:
                evidence = populate_workbook_original_values(
                    evidence, fixture.artifacts
                )
            materialize.assert_called_once()
            self.assertFalse(fixture.upload.exists())
            for major in (19, 20):
                for mode in ("local", "remote"):
                    with self.subTest(raw=raw, major=major, mode=mode):
                        path = _write_comparison(
                            self.root / f"output-{index}-{major}-{mode}",
                            evidence,
                            record,
                            decision,
                            major,
                            mode,
                        )
                        with closing(load_workbook(path)) as workbook:
                            sheet = workbook["Fields set aside"]
                            self.assertEqual(
                                tuple(sheet.cell(row, 5).value for row in (7, 8)),
                                (raw, "company"),
                            )
                            self.assertTrue(
                                all(
                                    sheet.cell(row, 5).data_type == "s"
                                    for row in (7, 8)
                                )
                            )
                            self.assertEqual(
                                sheet["A4"].value, "2 identified source cells"
                            )
                            self.assertIn("1 saved finding", sheet["G4"].value)
                            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 2)
                            self.assertNotIn("Unavailable", str(tuple(sheet.values)))
                        self.assertEqual(
                            json.loads((path.parent / "manifest.json").read_bytes())[
                                "target"
                            ]["odoo_version"],
                            f"{major}.0",
                        )

    def test_two_outcomes_share_physical_cells_without_double_counting_them(self):
        fixture = fixtures._fixture(
            self.root / "source", ("000745",), kind="many2one", shape="scoped"
        )
        relationship = fixture.mapping.relationships[0]
        mapping = replace(
            fixture.mapping,
            relationships=(
                relationship,
                replace(relationship, target_field="other_category_id"),
            ),
        )
        record, decision, fields = _comparison(
            fixture, ("category_id", "other_category_id")
        )
        evidence = populate_workbook_original_values(
            _plan(fixture, mapping=mapping, fields=fields), fixture.artifacts
        )
        self.assertEqual(len(evidence.fields_set_aside.findings), 2)
        path = _write_comparison(self.root / "output", evidence, record, decision)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 4)
            self.assertEqual(sheet["A4"].value, "2 identified source cells")
            self.assertIn("2 saved findings", sheet["G4"].value)
            self.assertEqual(
                sum(sheet.cell(row, 5).value == "000745" for row in range(7, 11)), 2
            )

    def test_missing_snapshot_keeps_verified_geometry_and_unavailable_values(self):
        fixture = fixtures._fixture(
            self.root / "source", ("",), kind="many2one", shape="scoped"
        )
        fixture = replace(fixture, sources=_Sources(fixture.selection, ()))
        evidence = _plan(fixture)
        self.assertEqual(evidence.original_source_requests, ())
        self.assertEqual(fixture.sources.calls, ["selection", "snapshots"])
        self.assertEqual(
            tuple(
                item.value.state
                for item in evidence.fields_set_aside.findings[0].source_inputs
            ),
            (OriginalValueState.UNAVAILABLE,) * 2,
        )
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("missing snapshot read"),
        ):
            evidence = populate_workbook_original_values(evidence, fixture.artifacts)
        path = fixtures._workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(
                tuple(sheet.cell(row, 5).value for row in (7, 8)),
                ("Unavailable", "Unavailable"),
            )
            self.assertEqual(sheet["A4"].value, "2 identified source cells")
            self.assertIn("1 saved finding", sheet["G4"].value)
            self.assertIn(
                "1 finding(s) have no available original value", sheet["A2"].value
            )

    def test_restricted_scoped_outcome_does_not_read_or_expand_unverified_geometry(
        self,
    ):
        fixture = fixtures._fixture(
            self.root / "source", ("",), kind="many2one", shape="scoped"
        )
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("restricted snapshot read"),
        ):
            evidence = populate_workbook_original_values(
                fixture.plan(restricted=True), fixture.artifacts
            )
        self.assertEqual(fixture.sources.calls, [])
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())
        path = fixtures._workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 1)
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertEqual(sheet["G7"].value, "Field finding")
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
            self.assertIn("1 saved finding", sheet["G4"].value)

    def test_mixed_generated_scope_rejects_the_whole_group_while_an_independent_field_can_recover(
        self,
    ):
        fixture = fixtures._fixture(
            self.root / "source", ("",), kind="many2one", shape="scoped"
        )
        physical = fixture.selection.datasets[0]
        effective = replace(
            physical,
            columns=(
                *physical.columns,
                SourceDatasetColumn(4, "Generated scope", _SYNTHETIC_SCOPE, "string"),
            ),
        )
        selection = replace(
            fixture.selection, datasets=(effective,), content_hash="sha256:" + "b" * 64
        )
        relation = fixture.mapping.relationships[0]
        mapping = replace(
            fixture.mapping,
            fields=(
                *fixture.mapping.fields,
                ScalarFieldMapping("other_name", fixtures._KEY),
            ),
            relationships=(
                replace(
                    relation,
                    source_column_keys=(fixtures._KEY, _SYNTHETIC_SCOPE),
                    resolver=replace(
                        relation.resolver,
                        scope_mappings=(
                            ReferenceKeyMapping(_SYNTHETIC_SCOPE, "company_id"),
                        ),
                    ),
                ),
            ),
        )
        finding = replace(
            fixture.fields.findings[0], source_keys=(fixtures._KEY, _SYNTHETIC_SCOPE)
        )
        independent = FieldSetAside(
            FieldSetAsideReason.MISSING,
            "partners",
            finding.source_row,
            (fixtures._KEY,),
            "other_name",
            "The direct input is blank.",
        )
        fields = replace(
            fixture.fields,
            findings=(finding, independent),
            source_labels={
                **fixture.fields.source_labels,
                ("partners", _SYNTHETIC_SCOPE): "Generated scope",
            },
        )
        fixture = replace(
            fixture,
            rows=tuple(
                replace(
                    row,
                    lineage=replace(
                        row.lineage,
                        source_selection_hash=selection.content_hash,
                        field_sources={
                            **row.lineage.field_sources,
                            relation.target_field: (fixtures._KEY, _SYNTHETIC_SCOPE),
                        },
                    ),
                )
                for row in fixture.rows
            ),
        )
        evidence = populate_workbook_original_values(
            _plan(fixture, selection=selection, mapping=mapping, fields=fields),
            fixture.artifacts,
        )
        scoped, direct = evidence.fields_set_aside.findings
        self.assertEqual(scoped.source_inputs, ())
        self.assertEqual(
            tuple(item.coordinate.source_key for item in direct.source_inputs), (fixtures._KEY,)
        )
        self.assertEqual(direct.source_inputs[0].value.state, OriginalValueState.BLANK)
        path = fixtures._workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(len(tuple(sheet.iter_rows(min_row=7))), 2)
            self.assertEqual(sheet["A4"].value, "1 identified source cell")
            self.assertIn("2 saved findings", sheet["G4"].value)

    def test_partial_cohort_cannot_inherit_a_complete_scoped_request(self):
        fixture = fixtures._fixture(
            self.root / "source", ("000745",), kind="many2one", shape="scoped"
        )
        _, _, fields = _comparison(fixture)
        evidence = _plan(fixture, fields=fields)
        proven = evidence.fields_set_aside.findings[0]
        partial = replace(
            proven,
            source_inputs=(
                replace(proven.source_inputs[0], value=OriginalSourceValue.unavailable()),
            ),
            detail="Unproven partial cohort.",
        )
        evidence = replace(
            evidence,
            fields_set_aside=replace(
                evidence.fields_set_aside, findings=(proven, partial)
            ),
        )
        hydrated = populate_workbook_original_values(evidence, fixture.artifacts)
        whole, incomplete = hydrated.fields_set_aside.findings
        self.assertEqual(
            tuple(item.value.text for item in whole.source_inputs),
            ("000745", "company"),
        )
        self.assertEqual(
            incomplete.source_inputs[0].value.state, OriginalValueState.UNAVAILABLE
        )
