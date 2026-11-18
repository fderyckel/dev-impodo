"""Bind derived outcomes to complete physical cohorts across source snapshots."""

from contextlib import closing
from cProfile import Profile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.reporting import write_review_workbook
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.preflight.fields_set_aside import FieldSetAside, FieldSetAsideReason
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceInput,
    OriginalSourceOccurrence,
    OriginalSourceRequest,
    OriginalSourceTarget,
    OriginalSourceValue,
    OriginalValueState,
)
from tests.integration.artifacts import test_fields_set_aside_coverage as workbooks
from tests.integration.artifacts import test_review_original_values as sources


class PhysicalOriginalValuesTests(TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary)
        self.artifacts = LocalArtifactStore(self.root / "artifacts")
        first, _ = sources._publish(
            self.artifacts,
            sources._frame(((2, "000045", "unused"),)),
            dataset_id="dataset:" + "a" * 24,
            dataset="Orders",
        )
        second, _ = sources._publish(
            self.artifacts,
            sources._frame(((8, "=1+2", "unused"),)),
            dataset_id="dataset:" + "b" * 24,
            dataset="Contacts",
        )
        self.snapshots = (first, second)
        self.coordinates = (
            OriginalSourceCoordinate(
                first.dataset_id, first.dataset_name, 2, sources._CODE
            ),
            OriginalSourceCoordinate(
                second.dataset_id, second.dataset_name, 8, sources._CODE
            ),
        )

    def finding(self, target="contact_id", trace="trace-one"):
        return FieldSetAside(
            FieldSetAsideReason.UNRECOGNIZED,
            "joined",
            3,
            ("derived:order_code", "derived:contact_code"),
            target,
            "The related record was not found.",
            tuple(
                OriginalSourceInput(coordinate, OriginalSourceValue.unavailable())
                for coordinate in self.coordinates
            ),
            source_trace_id=trace,
        )

    def evidence(self, findings):
        fields = replace(
            workbooks._fields(findings),
            target_models={"joined": "x.document"},
            source_labels={
                ("Orders", sources._CODE): "Order code",
                ("Contacts", sources._CODE): "Contact code",
            },
        )
        requests = []
        for snapshot, coordinate in zip(self.snapshots, self.coordinates, strict=True):
            targets = tuple(
                OriginalSourceTarget(
                    OriginalSourceOccurrence(
                        finding.dataset,
                        finding.source_row,
                        finding.target_field,
                        finding.source_keys,
                        finding.source_trace_id,
                    ),
                    coordinate,
                    self.coordinates,
                )
                for finding in findings
            )
            requests.append(OriginalSourceRequest(snapshot, (coordinate,), targets))
        return replace(
            workbooks._evidence(fields),
            dataset_labels={"joined": "Joined rows"},
            original_source_requests=tuple(requests),
        )

    def test_cross_snapshot_cohorts_read_each_snapshot_once_and_keep_target_identity(
        self,
    ):
        finding = self.finding()
        other = self.finding("backup_contact_id")
        unproven = self.finding(trace="another-trace")
        evidence = self.evidence((finding, other))
        evidence = replace(
            evidence,
            fields_set_aside=replace(
                evidence.fields_set_aside, findings=(finding, other, unproven)
            ),
        )
        with patch.object(
            self.artifacts,
            "materialize_source_snapshot",
            wraps=self.artifacts.materialize_source_snapshot,
        ) as materialize:
            hydrated = populate_workbook_original_values(evidence, self.artifacts)
        self.assertEqual(materialize.call_count, 2)
        first, second, unmatched = hydrated.fields_set_aside.findings
        self.assertEqual(
            tuple(item.value.text for item in first.source_inputs), ("000045", "=1+2")
        )
        self.assertEqual(
            tuple(item.coordinate for item in first.source_inputs), self.coordinates
        )
        self.assertIs(first.source_inputs[0].value, second.source_inputs[0].value)
        self.assertIs(first.source_inputs[1].value, second.source_inputs[1].value)
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for item in unmatched.source_inputs
            )
        )
        self.assertEqual(hydrated.original_source_requests, ())

    def test_incomplete_cross_snapshot_authorization_fails_before_any_read(self):
        evidence = self.evidence((self.finding(),))
        evidence = replace(
            evidence, original_source_requests=evidence.original_source_requests[:1]
        )
        with (
            patch.object(self.artifacts, "materialize_source_snapshot") as materialize,
            self.assertRaisesRegex(ReadinessError, "Prepare the data again"),
        ):
            populate_workbook_original_values(evidence, self.artifacts)
        materialize.assert_not_called()

    def test_reordered_physical_cohort_cannot_inherit_another_authorization(self):
        finding = self.finding()
        reversed_finding = replace(
            finding,
            source_inputs=tuple(reversed(finding.source_inputs)),
            detail="Different physical input order.",
        )
        evidence = self.evidence((finding,))
        evidence = replace(
            evidence,
            fields_set_aside=replace(
                evidence.fields_set_aside, findings=(finding, reversed_finding)
            ),
        )
        hydrated = populate_workbook_original_values(evidence, self.artifacts)
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.PRESENT
                for item in hydrated.fields_set_aside.findings[0].source_inputs
            )
        )
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for item in hydrated.fields_set_aside.findings[1].source_inputs
            )
        )

    def test_workbook_shows_physical_locations_and_preserves_logical_outcome_counts(
        self,
    ):
        evidence = populate_workbook_original_values(
            self.evidence((self.finding(), self.finding("backup_contact_id"))),
            self.artifacts,
        )
        manifest = workbooks._empty_manifest(self.root)
        output = write_review_workbook(
            manifest, self.root / "physical.xlsx", review_evidence=evidence
        )
        with closing(load_workbook(output)) as workbook:
            sheet = workbook["Fields set aside"]
            rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                {row[1:5] for row in rows},
                {
                    ("Orders", 2, "Order code", "000045"),
                    ("Contacts", 8, "Contact code", "=1+2"),
                },
            )
            self.assertEqual({row[9:11] for row in rows}, {("Joined rows", 3)})
            self.assertEqual(sheet["J6"].value, "Prepared dataset")
            self.assertEqual(sheet["K6"].value, "Prepared row")
            self.assertEqual(sheet["A4"].value, "2 identified source cells")
            self.assertEqual(sheet["G4"].value, "2 saved findings")
            self.assertEqual(sheet.freeze_panes, "A7")
            self.assertEqual(sheet.auto_filter.ref, "A6:K10")
            self.assertTrue(
                all(sheet.cell(row, 5).data_type == "s" for row in range(7, 11))
            )
            self.assertNotIn("Unavailable", str(rows))

    def test_same_row_and_key_in_different_files_remain_distinct_cells(self):
        coordinates = tuple(
            replace(coordinate, source_row=2) for coordinate in self.coordinates
        )
        finding = replace(
            self.finding(),
            source_inputs=tuple(
                OriginalSourceInput(coordinate, OriginalSourceValue.unavailable())
                for coordinate in coordinates
            ),
        )
        evidence = workbooks._evidence(workbooks._fields((finding,)))
        manifest = workbooks._empty_manifest(self.root)
        output = write_review_workbook(
            manifest, self.root / "same-row.xlsx", review_evidence=evidence
        )
        with closing(load_workbook(output)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["A4"].value, "2 identified source cells")
            rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
            self.assertEqual({row[1] for row in rows}, {"Orders", "Contacts"})
            self.assertEqual({row[2] for row in rows}, {2})
            self.assertEqual(sheet["G4"].value, "1 saved findings")

    def test_grouped_cohort_authorization_hash_work_grows_with_cells(self):
        count = 2_001
        snapshot, _ = sources._publish(
            self.artifacts,
            sources._frame(
                tuple((row, f"code-{row}", "unused") for row in range(2, count + 2))
            ),
            dataset_id="dataset:" + "c" * 24,
            dataset="Grouped input",
        )
        equality_checks = []

        class ObservedCohort(tuple):
            def __eq__(self, other):
                equality_checks.append(1)
                return super().__eq__(other)

            __hash__ = tuple.__hash__

        coordinates = ObservedCohort(
            OriginalSourceCoordinate(
                snapshot.dataset_id, snapshot.dataset_name, row, sources._CODE
            )
            for row in range(2, count + 2)
        )
        finding = replace(
            self.finding(),
            source_inputs=tuple(
                OriginalSourceInput(coordinate, OriginalSourceValue.unavailable())
                for coordinate in coordinates
            ),
        )
        occurrence = OriginalSourceOccurrence(
            finding.dataset,
            finding.source_row,
            finding.target_field,
            finding.source_keys,
            finding.source_trace_id,
        )
        request = OriginalSourceRequest(
            snapshot,
            coordinates,
            tuple(
                OriginalSourceTarget(occurrence, coordinate, coordinates)
                for coordinate in coordinates
            ),
        )
        evidence = replace(
            workbooks._evidence(workbooks._fields((finding,))),
            original_source_requests=(request,),
        )
        equality_checks.clear()
        profile = Profile()
        hydrated = profile.runcall(
            populate_workbook_original_values, evidence, self.artifacts
        )
        coordinate_hash = OriginalSourceCoordinate.__hash__.__code__
        hash_calls = sum(
            entry.callcount
            for entry in profile.getstats()
            if entry.code is coordinate_hash
        )
        # Count structural work, not elapsed time: validating and hydrating one
        # complete cohort needs only bounded passes over its physical cells.
        self.assertLessEqual(hash_calls, count * 16)
        self.assertLessEqual(len(equality_checks), 2)
        inputs = hydrated.fields_set_aside.findings[0].source_inputs
        self.assertEqual(len(inputs), count)
        self.assertEqual(inputs[0].value.text, "code-2")
        self.assertEqual(inputs[-1].value.text, f"code-{count + 1}")

    def test_constant_omission_has_no_invented_cell_or_unavailable_gap(self):
        finding = replace(self.finding(), source_inputs=(), source_inputs_known=True)
        evidence = replace(
            workbooks._evidence(workbooks._fields((finding,))),
            dataset_labels={"joined": "Joined rows"},
        )
        manifest = workbooks._empty_manifest(self.root)
        output = write_review_workbook(
            manifest, self.root / "constant.xlsx", review_evidence=evidence
        )
        with closing(load_workbook(output)) as workbook:
            sheet = workbook["Fields set aside"]
            rows = tuple(sheet.iter_rows(min_row=7, values_only=True))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1:5], (None, None, None, "No source cell"))
            self.assertEqual(rows[0][6], "No source cell")
            self.assertEqual(rows[0][9:11], ("Joined rows", 3))
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
            self.assertEqual(sheet["G4"].value, "1 saved findings")
            self.assertNotIn("unavailable", sheet["A2"].value.lower())
            self.assertNotIn("unverified", sheet["A2"].value.lower())
            self.assertNotIn("no verified", sheet["A2"].value.lower())

    def test_restricted_export_clears_physical_locations_and_known_empty_state(self):
        evidence = self.evidence((self.finding(),))
        evidence = replace(
            evidence,
            fields_set_aside=replace(
                evidence.fields_set_aside, source_values_restricted=True
            ),
        )
        with patch.object(self.artifacts, "materialize_source_snapshot") as materialize:
            hydrated = populate_workbook_original_values(evidence, self.artifacts)
        materialize.assert_not_called()
        finding = hydrated.fields_set_aside.findings[0]
        self.assertEqual(finding.source_inputs, ())
        self.assertFalse(finding.source_inputs_known)
        manifest = workbooks._empty_manifest(self.root)
        output = write_review_workbook(
            manifest, self.root / "restricted.xlsx", review_evidence=hydrated
        )
        with closing(load_workbook(output)) as workbook:
            sheet = workbook["Fields set aside"]
            self.assertEqual(sheet["E7"].value, "Withheld")
            self.assertEqual(sheet["A4"].value, "0 identified source cells")
            self.assertNotIn("Orders", str(tuple(sheet.values)))
            self.assertNotIn("Contacts", str(tuple(sheet.values)))
            self.assertEqual(sheet.max_column, 9)
