"""Verify bounded, private original-cell hydration from immutable snapshots."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import polars as pl

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.shared.artifacts import ArtifactStoreError
from impodo.domain.errors import ReadinessError
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceRequest,
    OriginalSourceTarget,
    OriginalSourceValue,
    OriginalValueState,
)
from impodo.domain.preflight.reports import ReviewWorkbookEvidence
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import (
    SOURCE_ROW_COLUMN,
    EncodedSourceCell,
    SourceCellKind,
    SourceSnapshot,
    SourceSnapshotColumn,
    SourceSnapshotSchema,
)

_OWNER = "11111111-1111-4111-8111-111111111111"
_CODE = "column:1:000000000001"
_OTHER = "column:2:000000000002"
_HASH = "sha256:" + "a" * 64
_SCHEMA = SourceSnapshotSchema.create(
    SourceSnapshotColumn.create(
        ordinal=index,
        stable_key=key,
        source_name=name,
        candidate_type="string",
    )
    for index, key, name in ((1, _CODE, "Code"), (2, _OTHER, "Other"))
)
_PHYSICAL_SCHEMA = {
    SOURCE_ROW_COLUMN: pl.Int64,
    "v_000001": pl.String,
    "k_000001": pl.UInt8,
    "v_000002": pl.String,
    "k_000002": pl.UInt8,
}


def _frame(rows: tuple[tuple[int, object, object], ...]) -> pl.DataFrame:
    encoded = []
    for row, value, other in rows:
        first, second = (
            EncodedSourceCell.from_python(value),
            EncodedSourceCell.from_python(other),
        )
        encoded.append(
            (row, first.text, int(first.kind), second.text, int(second.kind))
        )
    return pl.DataFrame(encoded, schema=_PHYSICAL_SCHEMA, orient="row")


def _publish(
    artifacts: LocalArtifactStore,
    frame: pl.DataFrame,
) -> tuple[SourceSnapshot, Path]:
    with artifacts.prepare_source_snapshot(_OWNER) as directory:
        candidate = directory / "source.parquet"
        frame.write_parquet(candidate, row_group_size=100)
        digest = "sha256:" + sha256(candidate.read_bytes()).hexdigest()
        snapshot = SourceSnapshot.create(
            data_version_id=_OWNER,
            dataset_id="dataset:" + "1" * 24,
            dataset_name="partners",
            source=FileSourceBinding(
                file_id="file-1",
                table_key="csv",
                source_sha256=_HASH,
                catalog_hash=_HASH,
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            physical_selection_hash=_HASH,
            schema=_SCHEMA,
            row_count=frame.height,
            data_logical_hash=_HASH,
            parquet_sha256=digest,
            created_at=datetime(2026, 11, 17, tzinfo=UTC),
        )
        artifacts.publish_source_snapshot(
            _OWNER, candidate, snapshot.parquet_storage_key, expected_sha256=digest
        )
    with artifacts.materialize_source_snapshot(
        _OWNER, snapshot.parquet_storage_key, expected_sha256=digest
    ) as path:
        return snapshot, path


def _finding(
    row: int | None,
    keys: tuple[str, ...] = (_CODE,),
    *,
    target_field: str = "ref",
    proven: bool = True,
) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.INVALID,
        "partners",
        row,
        keys,
        target_field,
        "The saved preparation could not use this value.",
        source_inputs=(
            tuple(
                OriginalSourceInput(key, OriginalSourceValue.unavailable())
                for key in keys
            )
            if proven and row is not None
            else ()
        ),
    )


def _evidence(
    snapshot: SourceSnapshot,
    cells: tuple[tuple[int, str], ...],
    *,
    findings: tuple[FieldSetAside, ...] | None = None,
    restricted: bool = False,
) -> ReviewWorkbookEvidence:
    fields = FieldSetAsideEvidence(
        findings=tuple(_finding(row, (key,)) for row, key in cells)
        if findings is None
        else findings,
        source_labels={},
        source_keys={},
        null_omissions={},
        upsert_datasets=frozenset(),
        target_models={},
        coverage_gaps=(),
        source_values_restricted=restricted,
    )
    return ReviewWorkbookEvidence(
        frozen_input_hash=_HASH,
        records=(),
        dataset_labels={},
        target_model_labels={},
        target_field_labels={},
        normalization_content_hash="",
        cell_effects=(),
        target_field_required={},
        fields_set_aside=fields,
        original_source_requests=(
            OriginalSourceRequest(
                snapshot,
                "partners",
                cells,
                tuple(
                    OriginalSourceTarget(row, key, "ref", (key,)) for row, key in cells
                ),
            ),
        ),
    )


class ReviewOriginalValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifacts = LocalArtifactStore(Path(self.temporary.name) / "artifacts")

    def test_preserves_exact_strings_and_distinguishes_captured_blanks(self) -> None:
        originals = ("001", "  café\u00a0  ", "=1+2", None, "")
        snapshot, _ = _publish(
            self.artifacts,
            _frame(
                tuple(
                    (row, value, "unused")
                    for row, value in enumerate(originals, start=2)
                )
            ),
        )
        cells = tuple((row, _CODE) for row in range(2, 7))
        evidence = _evidence(snapshot, cells)

        hydrated = populate_workbook_original_values(evidence, self.artifacts)

        assert hydrated is not None and hydrated.fields_set_aside is not None
        values = tuple(
            finding.source_inputs[0].value
            for finding in hydrated.fields_set_aside.findings
        )
        self.assertEqual(tuple(value.text for value in values), originals)
        self.assertEqual(
            tuple(value.state for value in values),
            (OriginalValueState.PRESENT,) * 3 + (OriginalValueState.BLANK,) * 2,
        )
        self.assertEqual(values[3].cell, EncodedSourceCell(SourceCellKind.NULL, None))
        self.assertEqual(values[4].cell, EncodedSourceCell(SourceCellKind.STRING, ""))
        self.assertEqual(hydrated.original_source_requests, ())
        assert evidence.fields_set_aside is not None
        self.assertTrue(
            all(
                finding.source_inputs[0].value.state is OriginalValueState.UNAVAILABLE
                for finding in evidence.fields_set_aside.findings
            )
        )
        self.assertEqual(len(evidence.original_source_requests), 1)

    def test_one_verified_read_merges_requests_without_guessing_other_findings(
        self,
    ) -> None:
        snapshot, _ = _publish(
            self.artifacts, _frame(((2, "001", "A"), (3, "002", "B")))
        )
        findings = (
            _finding(2),
            _finding(2, (_OTHER,)),
            _finding(3),
            _finding(None),
            _finding(2, (_CODE, _OTHER)),
            _finding(4),
        )
        evidence = _evidence(snapshot, ((2, _CODE),), findings=findings)
        evidence = replace(
            evidence,
            original_source_requests=(
                *evidence.original_source_requests,
                OriginalSourceRequest(
                    snapshot,
                    "partners",
                    ((2, _OTHER), (3, _CODE)),
                    (
                        OriginalSourceTarget(2, _OTHER, "ref", (_OTHER,)),
                        OriginalSourceTarget(3, _CODE, "ref", (_CODE,)),
                    ),
                ),
            ),
        )
        with patch.object(
            self.artifacts,
            "materialize_source_snapshot",
            wraps=self.artifacts.materialize_source_snapshot,
        ) as materialize:
            hydrated = populate_workbook_original_values(evidence, self.artifacts)

        materialize.assert_called_once_with(
            _OWNER,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        )
        assert hydrated is not None and hydrated.fields_set_aside is not None
        self.assertEqual(
            tuple(
                tuple(item.value.text for item in finding.source_inputs)
                for finding in hydrated.fields_set_aside.findings
            ),
            (("001",), ("A",), ("002",), (), (None, None), (None,)),
        )
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for finding in hydrated.fields_set_aside.findings[3:]
                for item in finding.source_inputs
            )
        )

    def test_target_proof_does_not_spread_to_unproven_formula_sharing_a_cell(
        self,
    ) -> None:
        snapshot, _ = _publish(self.artifacts, _frame(((2, "001", "unused"),)))
        direct = _finding(2)
        unproven_formula = _finding(2, target_field="computed_ref")
        evidence = _evidence(
            snapshot, ((2, _CODE),), findings=(direct, unproven_formula)
        )

        hydrated = populate_workbook_original_values(evidence, self.artifacts)

        assert hydrated is not None and hydrated.fields_set_aside is not None
        direct_result, formula_result = hydrated.fields_set_aside.findings
        self.assertEqual(direct_result.source_inputs[0].value.text, "001")
        self.assertEqual(formula_result, unproven_formula)
        self.assertEqual(
            formula_result.source_inputs[0].value.state, OriginalValueState.UNAVAILABLE
        )

    def test_complete_scoped_inputs_keep_order_and_values_with_one_snapshot_read(
        self,
    ) -> None:
        snapshot, _ = _publish(
            self.artifacts, _frame(((2, "001", "  EU  "), (3, "=1+2", None)))
        )
        cohort = (_OTHER, _CODE)
        findings = (
            _finding(2, cohort, target_field="contact_id"),
            _finding(3, cohort, target_field="contact_id"),
            _finding(2, target_field="code_ref"),
        )
        cells = ((2, _CODE), (2, _OTHER), (3, _CODE), (3, _OTHER))
        targets = tuple(
            OriginalSourceTarget(row, key, "contact_id", cohort)
            for row in (2, 3)
            for key in cohort
        ) + (OriginalSourceTarget(2, _CODE, "code_ref", (_CODE,)),)
        evidence = replace(
            _evidence(snapshot, cells, findings=findings),
            original_source_requests=(
                OriginalSourceRequest(snapshot, "partners", cells, targets),
            ),
        )
        with patch.object(
            self.artifacts,
            "materialize_source_snapshot",
            wraps=self.artifacts.materialize_source_snapshot,
        ) as materialize:
            hydrated = populate_workbook_original_values(evidence, self.artifacts)

        materialize.assert_called_once_with(
            _OWNER,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        )
        assert hydrated is not None and hydrated.fields_set_aside is not None
        result = hydrated.fields_set_aside.findings
        self.assertEqual(len(result), len(findings))
        self.assertEqual(
            tuple(item.source_key for item in result[0].source_inputs), cohort
        )
        self.assertEqual(
            tuple(
                tuple(item.value.text for item in finding.source_inputs)
                for finding in result
            ),
            (("  EU  ", "001"), (None, "=1+2"), ("001",)),
        )
        self.assertEqual(
            result[1].source_inputs[0].value.state, OriginalValueState.BLANK
        )
        self.assertEqual(
            tuple(finding.detail for finding in result),
            tuple(finding.detail for finding in findings),
        )
        self.assertIs(
            result[0].source_inputs[1].value, result[2].source_inputs[0].value
        )
        self.assertEqual(hydrated.original_source_requests, ())

    def test_authorization_matches_the_complete_ordered_cohort_without_guessing_inputs(
        self,
    ) -> None:
        snapshot, _ = _publish(self.artifacts, _frame(((2, "001", "scope"),)))
        cohorts = ((_CODE,), (_CODE, _OTHER), (_OTHER, _CODE))
        findings = tuple(_finding(2, keys) for keys in cohorts) + (
            _finding(2, (_CODE, _OTHER), proven=False),
        )
        for authorized in cohorts:
            with self.subTest(authorized=authorized):
                cells = tuple((2, key) for key in authorized)
                targets = tuple(
                    OriginalSourceTarget(2, key, "ref", authorized)
                    for key in authorized
                )
                evidence = replace(
                    _evidence(snapshot, cells, findings=findings),
                    original_source_requests=(
                        OriginalSourceRequest(snapshot, "partners", cells, targets),
                    ),
                )
                hydrated = populate_workbook_original_values(evidence, self.artifacts)
                assert hydrated is not None and hydrated.fields_set_aside is not None
                for before, after in zip(
                    findings, hydrated.fields_set_aside.findings, strict=True
                ):
                    if before.source_keys != authorized or not before.source_inputs:
                        self.assertEqual(after, before)
                    else:
                        self.assertTrue(
                            all(
                                item.value.state is OriginalValueState.PRESENT
                                for item in after.source_inputs
                            )
                        )
                        self.assertEqual(
                            tuple(item.value.text for item in after.source_inputs),
                            tuple(
                                "001" if key == _CODE else "scope" for key in authorized
                            ),
                        )
                self.assertEqual(
                    hydrated.fields_set_aside.findings[-1].source_inputs, ()
                )

    def test_only_selected_rows_and_columns_stream_in_bounded_batches(self) -> None:
        rows = tuple((row, f"code-{row}", "not-an-integer") for row in range(2, 1_104))
        frame = _frame(rows).with_columns(pl.lit(3, dtype=pl.UInt8).alias("k_000002"))
        snapshot, _ = _publish(self.artifacts, frame)
        cells = tuple((row, _CODE) for row in range(3, 1_104))
        evidence = _evidence(snapshot, cells)
        collect_batches = pl.LazyFrame.collect_batches
        batch_sizes: list[int] = []
        projected_columns: list[tuple[str, ...]] = []

        def observe_batches(query, **kwargs):
            self.assertEqual(
                kwargs,
                {"chunk_size": 1_000, "maintain_order": True, "engine": "streaming"},
            )
            for batch in collect_batches(query, **kwargs):
                batch_sizes.append(batch.height)
                projected_columns.append(tuple(batch.columns))
                yield batch

        with (
            patch.object(
                pl.LazyFrame,
                "collect_batches",
                autospec=True,
                side_effect=observe_batches,
            ),
            patch.object(
                pl.LazyFrame, "collect", side_effect=AssertionError("unbounded collect")
            ),
        ):
            hydrated = populate_workbook_original_values(evidence, self.artifacts)

        self.assertGreater(len(batch_sizes), 1)
        self.assertLessEqual(max(batch_sizes), 1_000)
        self.assertEqual(sum(batch_sizes), len(cells))
        self.assertTrue(
            all(
                columns == (SOURCE_ROW_COLUMN, "v_000001", "k_000001")
                for columns in projected_columns
            )
        )
        assert hydrated is not None and hydrated.fields_set_aside is not None
        self.assertEqual(
            hydrated.fields_set_aside.findings[0].source_inputs[0].value.text, "code-3"
        )
        self.assertEqual(
            hydrated.fields_set_aside.findings[-1].source_inputs[0].value.text,
            "code-1103",
        )

    def test_restricted_evidence_drops_requests_and_existing_originals_without_reading(
        self,
    ) -> None:
        snapshot, _ = _publish(self.artifacts, _frame(((2, "secret", "unused"),)))
        finding = replace(
            _finding(2, (_CODE, _OTHER)),
            source_inputs=tuple(
                OriginalSourceInput(
                    key,
                    OriginalSourceValue.from_cell(EncodedSourceCell.from_python(value)),
                )
                for key, value in (
                    (_CODE, "previously hydrated secret"),
                    (_OTHER, "private scope"),
                )
            ),
        )
        evidence = _evidence(
            snapshot, ((2, _CODE),), findings=(finding,), restricted=True
        )
        for requests in (evidence.original_source_requests, ()):
            with (
                self.subTest(requests=bool(requests)),
                patch.object(
                    self.artifacts,
                    "materialize_source_snapshot",
                    side_effect=AssertionError("private read"),
                ) as materialize,
            ):
                hydrated = populate_workbook_original_values(
                    replace(evidence, original_source_requests=requests), self.artifacts
                )
            materialize.assert_not_called()
            assert hydrated is not None and hydrated.fields_set_aside is not None
            self.assertEqual(hydrated.original_source_requests, ())
            self.assertEqual(
                hydrated.fields_set_aside.findings[0].source_inputs,
                (),
            )

    def test_absent_evidence_or_requests_does_not_open_a_snapshot(self) -> None:
        snapshot, _ = _publish(self.artifacts, _frame(((2, "001", "unused"),)))
        evidence = _evidence(snapshot, ((2, _CODE),))
        without_requests = replace(evidence, original_source_requests=())
        with patch.object(
            self.artifacts,
            "materialize_source_snapshot",
            side_effect=AssertionError("unexpected read"),
        ) as materialize:
            self.assertIsNone(populate_workbook_original_values(None, self.artifacts))
            self.assertIs(
                populate_workbook_original_values(without_requests, self.artifacts),
                without_requests,
            )
            without_fields = populate_workbook_original_values(
                replace(evidence, fields_set_aside=None), self.artifacts
            )
        materialize.assert_not_called()
        assert without_fields is not None
        self.assertEqual(without_fields.original_source_requests, ())

    def test_hash_tampering_remains_an_artifact_store_failure(self) -> None:
        snapshot, path = _publish(self.artifacts, _frame(((2, "001", "unused"),)))
        evidence = _evidence(snapshot, ((2, _CODE),))
        path.write_bytes(b"changed immutable bytes")
        with self.assertRaisesRegex(ArtifactStoreError, "hash verification"):
            populate_workbook_original_values(evidence, self.artifacts)

    def test_physical_schema_changes_require_preparation_again(self) -> None:
        frame = _frame(((2, "001", "unused"),)).with_columns(
            pl.col("k_000001").cast(pl.Int64)
        )
        snapshot, _ = _publish(self.artifacts, frame)
        with self.assertRaisesRegex(ReadinessError, "Prepare the data again"):
            populate_workbook_original_values(
                _evidence(snapshot, ((2, _CODE),)), self.artifacts
            )

    def test_missing_or_duplicate_requested_rows_require_preparation_again(
        self,
    ) -> None:
        for rows, wanted in (
            (((2, "001", "unused"),), 3),
            (((2, "001", "unused"), (2, "002", "unused")), 2),
        ):
            with self.subTest(rows=rows):
                snapshot, _ = _publish(self.artifacts, _frame(rows))
                with self.assertRaisesRegex(ReadinessError, "Prepare the data again"):
                    populate_workbook_original_values(
                        _evidence(snapshot, ((wanted, _CODE),)), self.artifacts
                    )

    def test_malformed_requested_cell_kinds_and_text_require_preparation_again(
        self,
    ) -> None:
        for text, kind in (("001", 3), ("secret", 255), ("not-null", 0), (None, 1)):
            with self.subTest(kind=kind, text=text):
                frame = _frame(((2, "001", "unused"),)).with_columns(
                    pl.lit(text, dtype=pl.String).alias("v_000001"),
                    pl.lit(kind, dtype=pl.UInt8).alias("k_000001"),
                )
                snapshot, _ = _publish(self.artifacts, frame)
                with self.assertRaisesRegex(ReadinessError, "Prepare the data again"):
                    populate_workbook_original_values(
                        _evidence(snapshot, ((2, _CODE),)), self.artifacts
                    )

    def test_conflicting_snapshots_for_one_dataset_fail_before_reading(self) -> None:
        first, _ = _publish(self.artifacts, _frame(((2, "first", "unused"),)))
        second, _ = _publish(self.artifacts, _frame(((2, "second", "unused"),)))
        evidence = _evidence(first, ((2, _CODE),))
        evidence = replace(
            evidence,
            original_source_requests=(
                *evidence.original_source_requests,
                OriginalSourceRequest(
                    second,
                    "partners",
                    ((2, _CODE),),
                    (OriginalSourceTarget(2, _CODE, "ref", (_CODE,)),),
                ),
            ),
        )
        with (
            patch.object(self.artifacts, "materialize_source_snapshot") as materialize,
            self.assertRaisesRegex(ReadinessError, "Prepare the data again"),
        ):
            populate_workbook_original_values(evidence, self.artifacts)
        materialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
