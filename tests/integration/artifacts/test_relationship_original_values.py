"""Preserve proven saved relationship inputs through snapshot and workbook export."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import load_workbook

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.data_version.source_files import load_source_tables
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotCandidateWriter,
    source_snapshot_schema,
)
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAsideEvidence,
    FieldSetAsideReason,
    field_set_aside_evidence,
)
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.domain.preflight.reports import ReviewWorkbookEvidence
from impodo.domain.preparation.source import prepare_source_tables
from impodo.domain.preparation.staging_contracts import (
    CanonicalRow,
    canonical_row_from_prepared,
)
from impodo.domain.recipe.profile import ProfileDocument
from impodo.domain.shared.models import (
    Classification,
    Decision,
    Issue,
    PreflightResult,
    PreparedRecord,
    TargetFingerprint,
    canonical_json_bytes,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_NOW = datetime(2026, 11, 17, tzinfo=UTC)
_HASH = "sha256:" + "a" * 64
_OWNER = "11111111-1111-4111-8111-111111111111"
_DATASET = "dataset:" + "1" * 24
_ID = "column:1:000000000001"
_SCOPE = "column:2:000000000002"
_KEY = "column:3:000000000003"
_COLUMNS = ((_ID, "Record"), (_SCOPE, "Scope"), (_KEY, "Relationship"))


class _Sources:
    def __init__(self, selection: SourceSelection, snapshot: SourceSnapshot) -> None:
        self.selection = selection
        self.snapshot = snapshot
        self.calls: list[str] = []

    def get_source_selection(self, workspace_id: str) -> SourceSelection:
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append("snapshots")
        return (self.snapshot,)


@dataclass(frozen=True)
class _Fixture:
    selection: SourceSelection
    mapping: DatasetMapping
    fields: FieldSetAsideEvidence
    rows: tuple[CanonicalRow, ...]
    records: tuple[PreparedRecord, ...]
    sources: _Sources
    artifacts: LocalArtifactStore
    upload: Path

    def plan(self, *, restricted: bool = False) -> ReviewWorkbookEvidence:
        fields, requests = plan_workbook_original_values(
            fields=self.fields,
            selection=self.selection,
            physical_selection_hash=self.selection.content_hash,
            mappings=(self.mapping,),
            sources=self.sources,
            workspace_id="workspace-1",
            restricted=restricted,
        )
        return ReviewWorkbookEvidence(
            frozen_input_hash=_HASH,
            records=(),
            dataset_labels={"partners": "Partners"},
            target_model_labels={},
            target_field_labels={},
            normalization_content_hash="",
            cell_effects=(),
            target_field_required={},
            fields_set_aside=fields,
            original_source_requests=requests,
        )


def _fixture(
    directory: Path,
    values: tuple[str | None, ...],
    *,
    kind: str = "many2many",
    shape: str = "direct",
) -> _Fixture:
    directory.mkdir(parents=True, exist_ok=True)
    upload = directory / "uploaded.csv"
    with upload.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([name for _, name in _COLUMNS])
        for index, value in enumerate(values, 1):
            prefix = [f"row-{index}", "company"]
            writer.writerow(prefix if value is None else [*prefix, value])
    target = "category_ids" if kind == "many2many" else "category_id"
    keys = (_KEY,) if shape == "direct" else (_KEY, _SCOPE)
    names = ("Relationship",) if shape == "direct" else ("Relationship", "Scope")
    resolver = RelationshipResolver(
        ResolverOrigin.TARGET_CATALOG,
        model="res.partner.category",
        key_mappings=(ReferenceKeyMapping(_KEY, "name"),)
        + ((ReferenceKeyMapping(_SCOPE, "code"),) if shape == "multiple" else ()),
        scope_mappings=(ReferenceKeyMapping(_SCOPE, "company_id"),)
        if shape == "scoped"
        else (),
    )
    relationship = RelationshipMapping(target, kind, keys, resolver, required=True)
    profile = ProfileDocument.model_validate(
        {
            "profile": {"id": "relationship_originals"},
            "datasets": [
                {
                    "name": "partners",
                    "source": {"file": upload.name, "encoding": "utf-8"},
                    "target": {"model": "res.partner", "mode": "upsert"},
                    "source_identity": {"fields": ["Record"]},
                    "target_identity": {
                        "components": [
                            {
                                "source_fields": ["Record"],
                                "target_fields": ["ref"],
                                "type": "string",
                            }
                        ]
                    },
                    "fields": {"name": {"source": "Record", "type": "string"}},
                    "relations": {
                        target: {
                            "kind": kind,
                            "source_fields": names,
                            "required": True,
                            "resolve": {
                                "target_model": "res.partner.category",
                                "target_fields": ["name", "code"]
                                if shape == "multiple"
                                else ["name"],
                                "target_scope_fields": ["company_id"]
                                if shape == "scoped"
                                else [],
                            },
                        }
                    },
                }
            ],
        }
    )
    plan = compile_profile_document(profile)
    (table,) = load_source_tables(plan, directory)
    dataset = SourceDataset(
        _DATASET,
        "partners",
        FileSourceBinding("file-1", "csv", table.content_hash, _HASH, "utf-8", ",", 1),
        len(table.rows),
        tuple(
            SourceDatasetColumn(index, name, key, "string")
            for index, (key, name) in enumerate(_COLUMNS, 1)
        ),
    )
    selection = SourceSelection(
        "selection-1", 1, _OWNER, _NOW, "Data manager", (dataset,), _HASH
    )
    mapping = DatasetMapping(
        dataset_id=_DATASET,
        target_model="res.partner",
        source_identity_column_keys=(_ID,),
        fields=(ScalarFieldMapping("name", _ID),),
        relationships=(relationship,),
    )
    prepared = prepare_source_tables(
        plan, (table,), source_hashes={"partners": table.content_hash}
    )
    rows = tuple(
        canonical_row_from_prepared(
            record,
            mode="upsert",
            source_hash=table.content_hash,
            source_selection_hash=selection.content_hash,
            mapping_hash=_HASH,
            schema_hash=_HASH,
            derived_plan_hash=None,
            field_sources={"name": (_ID,), target: keys},
            physical_dataset_id=_DATASET,
            physical_source_rows=(record.source_row,),
        )
        for record in prepared.records
    )
    saved_rows = tuple(CanonicalRow.from_dict(row.to_portable_dict()) for row in rows)
    fields = field_set_aside_evidence(
        plan,
        saved_rows,
        {("partners", key): name for key, name in _COLUMNS},
        {("partners", "name"): (_ID,), ("partners", target): keys},
        column_exclusions={},
    )
    artifacts = LocalArtifactStore(directory / "artifacts")
    schema = source_snapshot_schema(dataset)
    with artifacts.prepare_source_snapshot(_OWNER) as workspace:
        writer = SourceSnapshotCandidateWriter(workspace, schema, batch_rows=1_000)
        writer.append_source_rows(table.rows)
        candidate = writer.finalize()
        snapshot = SourceSnapshot.create(
            data_version_id=_OWNER,
            dataset_id=_DATASET,
            dataset_name=dataset.name,
            source=dataset.source,
            physical_selection_hash=selection.content_hash,
            schema=schema,
            row_count=candidate.row_count,
            data_logical_hash=candidate.data_logical_hash,
            parquet_sha256=candidate.parquet_sha256,
            created_at=_NOW,
        )
        artifacts.publish_source_snapshot(
            _OWNER,
            candidate.path,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        )
    records = tuple(
        replace(record, source_trace_id=row.row_id)
        for record, row in zip(prepared.records, saved_rows, strict=True)
    )
    upload.unlink()
    return _Fixture(
        selection,
        mapping,
        fields,
        saved_rows,
        records,
        _Sources(selection, snapshot),
        artifacts,
        upload,
    )


def _workbook(
    directory: Path,
    evidence: ReviewWorkbookEvidence,
    *,
    major: int = 20,
    mode: str = "local",
    decisions: tuple[Decision, ...] = (),
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    result = PreflightResult(
        profile_id="relationship_originals",
        source_hashes={"partners": _HASH},
        fingerprint=TargetFingerprint(
            _HASH, mode, "test", f"{major}.0", _NOW.isoformat()
        ),
        metadata_snapshot_hash=None,
        record_snapshot_hash=None,
        decisions=decisions,
        reference_resolutions=(),
        issues=(),
    )
    manifest = result.to_portable_dict()
    manifest["preflight_evidence"] = {"frozen_input_hash": _HASH}
    path = directory / "readiness_manifest.json"
    path.write_bytes(canonical_json_bytes(manifest))
    return write_review_workbook(
        path, directory / "review.xlsx", review_evidence=evidence
    )


class RelationshipOriginalValueExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_saved_many2many_invalid_items_keep_the_whole_literal_source_after_upload_removal(
        self,
    ) -> None:
        originals = ("001;;002", "  001 ; ; 002  ", "=1+2;;003")
        fixture = _fixture(self.root / "source", originals)
        self.assertFalse(fixture.upload.exists())
        self.assertTrue(
            all(
                issue.code == "SOURCE_TYPE_INVALID" and issue.field == "category_ids"
                for row in fixture.rows
                for issue in row.issues
            )
        )
        self.assertEqual(
            tuple(finding.reason for finding in fixture.fields.findings),
            (FieldSetAsideReason.INVALID,) * 3,
        )
        evidence = fixture.plan()
        self.assertEqual(
            evidence.original_source_requests[0].cells,
            ((2, _KEY), (3, _KEY), (4, _KEY)),
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
            hydrated = populate_workbook_original_values(evidence, fixture.artifacts)
        materialize.assert_called_once()
        self.assertEqual(fixture.sources.calls, ["selection", "snapshots"])
        assert hydrated is not None
        for major in (19, 20):
            for mode in ("local", "remote"):
                with self.subTest(major=major, mode=mode):
                    path = _workbook(
                        self.root / f"{major}-{mode}", hydrated, major=major, mode=mode
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        cells = tuple(sheet.cell(row, 5) for row in range(7, 10))
                        self.assertEqual(tuple(cell.value for cell in cells), originals)
                        self.assertTrue(all(cell.data_type == "s" for cell in cells))
                        self.assertEqual(sheet["D7"].value, "Relationship")
                        self.assertEqual(
                            sheet["A7"].value, FieldSetAsideReason.INVALID.value
                        )
                        self.assertIn("3 saved findings", sheet["G4"].value)

    def test_actual_required_many2one_blanks_keep_null_empty_and_whitespace_evidence(
        self,
    ) -> None:
        fixture = _fixture(self.root / "source", (None, "", "   "), kind="many2one")
        self.assertTrue(
            all(
                issue.code == "SOURCE_REQUIRED_VALUE_MISSING"
                and issue.field == "category_id"
                for row in fixture.rows
                for issue in row.issues
            )
        )
        evidence = populate_workbook_original_values(fixture.plan(), fixture.artifacts)
        assert evidence is not None and evidence.fields_set_aside is not None
        self.assertTrue(
            all(
                tuple(item.source_key for item in finding.source_inputs) == (_KEY,)
                for finding in evidence.fields_set_aside.findings
            )
        )
        originals = tuple(
            item.value
            for finding in evidence.fields_set_aside.findings
            for item in finding.source_inputs
        )
        self.assertEqual(tuple(value.text for value in originals), (None, "", "   "))
        self.assertEqual(
            tuple(value.state for value in originals),
            (
                OriginalValueState.BLANK,
                OriginalValueState.BLANK,
                OriginalValueState.PRESENT,
            ),
        )
        path = _workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertIsNone(sheet["E7"].value)
            self.assertIsNone(sheet["E8"].value)
            self.assertEqual(sheet["E9"].value, "   ")
            self.assertIn("blank cell", sheet["E7"].comment.text)
            self.assertIn("empty text", sheet["E8"].comment.text)
            self.assertEqual(sheet["A7"].value, FieldSetAsideReason.MISSING.value)

    def test_restricted_relationships_read_no_sources_and_never_export_the_raw_value(
        self,
    ) -> None:
        secret = "PrivateRelationship001;;Other"
        fixture = _fixture(self.root / "source", (secret,))
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("restricted original read"),
        ) as materialize:
            evidence = populate_workbook_original_values(
                fixture.plan(restricted=True), fixture.artifacts
            )
        materialize.assert_not_called()
        self.assertEqual(fixture.sources.calls, [])
        assert evidence is not None and evidence.fields_set_aside is not None
        self.assertEqual(
            evidence.fields_set_aside.findings[0].source_inputs,
            (),
        )
        path = _workbook(self.root / "output", evidence)
        with closing(load_workbook(path)) as workbook:
            self.assertEqual(workbook["Fields set aside"]["E7"].value, "Withheld")
            self.assertIn("withheld", workbook["Fields set aside"]["A2"].value)
        with ZipFile(path) as archive:
            self.assertNotIn(
                secret,
                "".join(
                    archive.read(name).decode()
                    for name in archive.namelist()
                    if name.endswith(".xml")
                ),
            )

    def test_multiple_key_inputs_remain_unavailable_without_source_reads(self) -> None:
        fixture = _fixture(
            self.root / "multiple", ("",), kind="many2one", shape="multiple"
        )
        self.assertEqual(fixture.fields.findings[0].source_keys, (_KEY, _SCOPE))
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("unproven input read"),
        ) as materialize:
            evidence = populate_workbook_original_values(
                fixture.plan(), fixture.artifacts
            )
        materialize.assert_not_called()
        self.assertEqual(fixture.sources.calls, [])
        assert evidence is not None and evidence.fields_set_aside is not None
        self.assertEqual(evidence.fields_set_aside.findings[0].source_inputs, ())
        path = _workbook(self.root / "multiple-output", evidence)
        with closing(load_workbook(path)) as workbook:
            self.assertEqual(workbook["Fields set aside"]["E7"].value, "Unavailable")
            self.assertIn(
                "no verified source-cell coordinates",
                workbook["Fields set aside"]["A2"].value,
            )

    def test_later_unknown_relationship_enters_only_from_the_comparison_manifest(
        self,
    ) -> None:
        fixture = _fixture(self.root / "source", ("001",), kind="many2one")
        self.assertEqual(fixture.fields.findings, ())
        with patch.object(
            fixture.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("comparison-only original read"),
        ) as materialize:
            evidence = populate_workbook_original_values(
                fixture.plan(), fixture.artifacts
            )
        materialize.assert_not_called()
        self.assertEqual(fixture.sources.calls, [])
        assert evidence is not None
        record = fixture.records[0]
        evidence = replace(evidence, records=(record,))
        decision = Decision(
            dataset=record.dataset,
            source_row=record.source_row,
            business_identity=record.target_identity,
            business_scope=record.target_scope,
            classification=Classification.BLOCKED,
            target_match_count=0,
            source_trace_id=record.source_trace_id,
            issues=(
                Issue(
                    "REFERENCE_NOT_FOUND",
                    "No related Odoo record matches the key.",
                    dataset=record.dataset,
                    row=record.source_row,
                    field="category_id",
                ),
            ),
        )
        for major in (19, 20):
            for mode in ("local", "remote"):
                with self.subTest(major=major, mode=mode):
                    path = _workbook(
                        self.root / f"{major}-{mode}",
                        evidence,
                        major=major,
                        mode=mode,
                        decisions=(decision,),
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        self.assertEqual(
                            sheet["A7"].value, FieldSetAsideReason.UNRECOGNIZED.value
                        )
                        self.assertEqual(sheet["E7"].value, "Unavailable")
                        self.assertEqual(sheet["D7"].value, "Relationship")
                        self.assertIn(
                            "1 finding(s) have no available original", sheet["A2"].value
                        )
                    manifest = json.loads(
                        (path.parent / "readiness_manifest.json").read_text()
                    )
                    self.assertEqual(manifest["target"]["odoo_version"], f"{major}.0")
                    self.assertEqual(manifest["target"]["connection_mode"], mode)


if __name__ == "__main__":
    unittest.main()
