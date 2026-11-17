"""Bound direct original-cell requests to verified Final review source evidence."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceLookupMapping,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleSet,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceValue,
    OriginalValueState,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.source_binding import DerivedSourceBinding, FileSourceBinding
from impodo.domain.source_snapshot import EncodedSourceCell, SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_NOW = datetime(2026, 11, 17, tzinfo=UTC)
_HASH = "sha256:" + "a" * 64
_OTHER_HASH = "sha256:" + "b" * 64
_KEY = "column:1:" + "1" * 12
_OTHER_KEY = "column:2:" + "2" * 12


class _Sources:
    def __init__(
        self, selection: SourceSelection | None, snapshots: tuple[SourceSnapshot, ...]
    ) -> None:
        self.selection = selection
        self.snapshots = snapshots
        self.calls: list[tuple[str, str]] = []

    def get_mapping_source_selection(self, workspace_id: str) -> SourceSelection | None:
        raise AssertionError("The effective frozen selection must not be reloaded")

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        self.calls.append(("selection", workspace_id))
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append(("snapshots", workspace_id))
        return self.snapshots


def _selection() -> SourceSelection:
    return SourceSelection(
        selection_id="selection-1",
        version=1,
        data_version_id="data-version-1",
        created_at=_NOW,
        created_by="Data manager",
        datasets=(
            SourceDataset(
                dataset_id="dataset:" + "1" * 24,
                name="Customers",
                source=FileSourceBinding(
                    "file-1", "Customers.csv", _HASH, _HASH, "utf-8", ",", 1
                ),
                row_count=3,
                columns=(
                    SourceDatasetColumn(1, "Name", _KEY, "string"),
                    SourceDatasetColumn(2, "Comment", _OTHER_KEY, "string"),
                ),
            ),
        ),
        content_hash=_HASH,
    )


def _snapshot(
    selection: SourceSelection, dataset: SourceDataset | None = None
) -> SourceSnapshot:
    dataset = dataset or selection.datasets[0]
    return SourceSnapshot.create(
        data_version_id=selection.data_version_id,
        dataset_id=dataset.dataset_id,
        dataset_name=dataset.name,
        source=dataset.source,
        physical_selection_hash=selection.content_hash,
        schema=source_snapshot_schema(dataset),
        row_count=dataset.row_count,
        data_logical_hash=_HASH,
        parquet_sha256=_OTHER_HASH,
        created_at=_NOW,
    )


def _finding(
    *, row: int | None = 1, target: str = "name", keys: tuple[str, ...] = (_KEY,)
) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.INVALID,
        "Customers",
        row,
        keys,
        target,
        "Value was omitted.",
    )


def _fields(*findings: FieldSetAside) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=tuple(findings),
        source_labels={("Customers", _KEY): "Name"},
        source_keys={("Customers", "name"): (_KEY,)},
        null_omissions={},
        upsert_datasets=frozenset({"Customers"}),
        target_models={"Customers": "res.partner"},
        coverage_gaps=("Original source values are not retained for every omission.",),
    )


def _mapping(selection: SourceSelection, *fields: ScalarFieldMapping) -> DatasetMapping:
    return DatasetMapping(
        dataset_id=selection.datasets[0].dataset_id,
        target_model="res.partner",
        fields=fields or (ScalarFieldMapping("name", _KEY),),
    )


class WorkbookOriginalValuePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _selection()
        self.snapshot = _snapshot(self.selection)
        self.sources = _Sources(self.selection, (self.snapshot,))

    def plan(self, fields: FieldSetAsideEvidence | None, **changes):
        options = {
            "fields": fields,
            "selection": self.selection,
            "physical_selection_hash": _HASH,
            "mappings": (_mapping(self.selection),),
            "sources": self.sources,
            "workspace_id": "workspace-1",
            "restricted": False,
        }
        options.update(changes)
        return plan_workbook_original_values(**options)

    def test_absent_and_empty_findings_do_not_open_source_evidence(self) -> None:
        self.assertEqual(self.plan(None), (None, ()))
        fields = _fields()
        self.assertEqual(self.plan(fields), (fields, ()))
        self.assertEqual(self.sources.calls, [])

    def test_restricted_originals_are_withheld_before_any_source_read(self) -> None:
        original = OriginalSourceValue.from_cell(
            EncodedSourceCell.from_python("Private customer")
        )
        fields = _fields(replace(_finding(), source_value=original), _finding(row=None))
        evidence, requests = self.plan(fields, restricted=True, selection=None)
        assert evidence is not None
        self.assertEqual(requests, ())
        self.assertTrue(evidence.source_values_restricted)
        self.assertEqual(
            evidence.findings[0].source_value.state, OriginalValueState.WITHHELD
        )
        self.assertIsNone(evidence.findings[0].source_value.cell)
        self.assertEqual(evidence.findings[1], fields.findings[1])
        self.assertEqual(self.sources.calls, [])

    def test_restricted_empty_evidence_keeps_its_policy_flag(self) -> None:
        evidence, requests = self.plan(_fields(), restricted=True)
        assert evidence is not None
        self.assertTrue(evidence.source_values_restricted)
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_direct_cells_are_sorted_deduplicated_and_read_in_one_bulk_capture(
        self,
    ) -> None:
        mapping = _mapping(
            self.selection,
            ScalarFieldMapping("name", _KEY),
            ScalarFieldMapping("comment", _KEY),
        )
        fields = _fields(
            *(
                _finding(row=3 if i % 2 else 1, target="comment" if i % 3 else "name")
                for i in range(100)
            )
        )
        evidence, requests = self.plan(fields, mappings=(mapping,))
        self.assertIs(evidence, fields)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].snapshot, self.snapshot)
        self.assertEqual(requests[0].dataset, "Customers")
        self.assertEqual(requests[0].cells, ((1, _KEY), (3, _KEY)))
        self.assertEqual(
            requests[0].targets,
            (
                (1, _KEY, "comment"),
                (1, _KEY, "name"),
                (3, _KEY, "comment"),
                (3, _KEY, "name"),
            ),
        )
        self.assertEqual(
            self.sources.calls,
            [("selection", "workspace-1"), ("snapshots", "workspace-1")],
        )
        self.assertTrue(
            all(
                finding.source_value.state is OriginalValueState.UNAVAILABLE
                for finding in evidence.findings
            )
        )

    def test_fallback_and_local_transforms_still_refer_to_the_exact_source_cell(
        self,
    ) -> None:
        field = ScalarFieldMapping(
            "name",
            _KEY,
            ScalarValueSource.SOURCE_WITH_FALLBACK,
            "Unknown",
            transform=ScalarTransformPolicy(trim=True, empty_as_null=True),
        )
        _, requests = self.plan(
            _fields(_finding()), mappings=(_mapping(self.selection, field),)
        )
        self.assertEqual(requests[0].cells, ((1, _KEY),))

    def test_composed_or_unproven_findings_do_not_read_snapshots(self) -> None:
        fields = (
            ScalarFieldMapping(
                "name", _KEY, transform=ScalarTransformPolicy(formula="upper(value)")
            ),
            ScalarFieldMapping(
                "name", value_source=ScalarValueSource.CONSTANT, literal_value="Unknown"
            ),
            ScalarFieldMapping(
                "name",
                value_source=ScalarValueSource.CONCATENATE,
                concatenation=ScalarConcatenation((_KEY, _OTHER_KEY)),
            ),
        )
        for field in fields:
            with self.subTest(field=field):
                _, requests = self.plan(
                    _fields(_finding()), mappings=(_mapping(self.selection, field),)
                )
                self.assertEqual(requests, ())
        for finding in (
            _finding(row=None),
            _finding(target="relationship_id"),
            _finding(keys=(_KEY, _OTHER_KEY)),
            _finding(keys=(_OTHER_KEY,)),
        ):
            with self.subTest(finding=finding):
                _, requests = self.plan(_fields(finding))
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_reference_and_selection_rules_do_not_establish_one_original_cell(
        self,
    ) -> None:
        reference = ScalarFieldMapping(
            "name",
            _KEY,
            reference_lookup=ReferenceLookupMapping(
                "11111111-1111-1111-1111-111111111111", _HASH, (_KEY,), "name"
            ),
        )
        conditional = ScalarFieldMapping(
            "name",
            value_source=ScalarValueSource.CONDITIONAL_RULES,
            selection_rules=SelectionRuleSet(
                (
                    SelectionRule(
                        "11111111-1111-1111-1111-111111111111",
                        (
                            SelectionCondition(
                                "22222222-2222-2222-2222-222222222222",
                                _KEY,
                                SelectionConditionOperator.IS_NOT_BLANK,
                            ),
                        ),
                        "active",
                    ),
                )
            ),
        )
        for field in (reference, conditional):
            with self.subTest(field=field):
                _, requests = self.plan(
                    _fields(_finding()), mappings=(_mapping(self.selection, field),)
                )
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_derived_datasets_are_unavailable_without_physical_reads(self) -> None:
        derived = replace(
            self.selection.datasets[0],
            source=DerivedSourceBinding(_HASH, ("input-dataset",), _HASH),
        )
        _, requests = self.plan(
            _fields(_finding()), selection=replace(self.selection, datasets=(derived,))
        )
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_missing_frozen_selection_or_hash_keeps_unavailable_coverage(self) -> None:
        fields = _fields(_finding())
        for changes in ({"selection": None}, {"physical_selection_hash": ""}):
            with self.subTest(changes=changes):
                evidence, requests = self.plan(fields, **changes)
                assert evidence is not None
                self.assertEqual(requests, ())
                self.assertEqual(evidence.findings, fields.findings)
                self.assertIn("selection was not recorded", evidence.coverage_gaps[-1])
        self.assertEqual(self.sources.calls, [])

    def test_missing_physical_selection_does_not_read_snapshot_manifests(self) -> None:
        self.sources.selection = None
        evidence, requests = self.plan(_fields(_finding()))
        assert evidence is not None
        self.assertEqual(requests, ())
        self.assertIn("selection was not recorded", evidence.coverage_gaps[-1])
        self.assertEqual(self.sources.calls, [("selection", "workspace-1")])

    def test_missing_snapshot_keeps_findings_and_explicit_unavailable_coverage(
        self,
    ) -> None:
        self.sources.snapshots = ()
        fields = _fields(_finding())
        evidence, requests = self.plan(fields)
        assert evidence is not None
        self.assertEqual(requests, ())
        self.assertEqual(evidence.findings, fields.findings)
        self.assertIn("snapshots are missing", evidence.coverage_gaps[-1])
        evidence_again, _ = self.plan(evidence)
        self.assertEqual(evidence_again.coverage_gaps, evidence.coverage_gaps)

    def test_missing_snapshot_does_not_discard_another_datasets_available_request(
        self,
    ) -> None:
        second = replace(
            self.selection.datasets[0],
            dataset_id="dataset:" + "2" * 24,
            name="Suppliers",
        )
        selection = replace(self.selection, datasets=(*self.selection.datasets, second))
        self.sources.selection = selection
        fields = replace(
            _fields(_finding(), replace(_finding(row=2), dataset="Suppliers")),
            target_models={"Customers": "res.partner", "Suppliers": "res.partner"},
        )
        mappings = (
            _mapping(selection),
            replace(_mapping(selection), dataset_id=second.dataset_id),
        )
        evidence, requests = self.plan(fields, selection=selection, mappings=mappings)
        assert evidence is not None
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].dataset, "Customers")
        self.assertEqual(evidence.findings, fields.findings)
        self.assertIn("snapshots are missing", evidence.coverage_gaps[-1])
        self.assertEqual(
            self.sources.calls,
            [("selection", "workspace-1"), ("snapshots", "workspace-1")],
        )

    def test_changed_physical_hash_or_data_version_is_rejected_before_snapshots(
        self,
    ) -> None:
        for physical in (
            replace(self.selection, content_hash=_OTHER_HASH),
            replace(self.selection, data_version_id="other-version"),
        ):
            with self.subTest(physical=physical):
                self.sources = _Sources(physical, (self.snapshot,))
                with self.assertRaises(ReadinessError):
                    self.plan(_fields(_finding()))
                self.assertEqual(self.sources.calls, [("selection", "workspace-1")])

    def test_changed_effective_physical_dataset_binding_is_rejected(self) -> None:
        for dataset in (
            replace(self.selection.datasets[0], row_count=2),
            replace(self.selection.datasets[0], name="Other"),
            replace(
                self.selection.datasets[0],
                columns=self.selection.datasets[0].columns[:1],
            ),
        ):
            with self.subTest(dataset=dataset):
                self.sources.selection = replace(self.selection, datasets=(dataset,))
                with self.assertRaises(ReadinessError):
                    self.plan(_fields(_finding()))

    def test_manifest_binding_mismatch_and_duplicate_manifests_are_rejected(
        self,
    ) -> None:
        unrelated_selection = replace(self.selection, data_version_id="other-version")
        mismatched_snapshot = _snapshot(unrelated_selection)
        changed_snapshots = (
            _snapshot(replace(self.selection, content_hash=_OTHER_HASH)),
            _snapshot(self.selection, replace(self.selection.datasets[0], row_count=2)),
            _snapshot(
                self.selection, replace(self.selection.datasets[0], name="Other")
            ),
            _snapshot(
                self.selection,
                replace(
                    self.selection.datasets[0],
                    columns=self.selection.datasets[0].columns[:1],
                ),
            ),
            _snapshot(
                self.selection,
                replace(
                    self.selection.datasets[0],
                    source=replace(
                        self.selection.datasets[0].source, file_id="other-file"
                    ),
                ),
            ),
        )
        for snapshots in (
            (mismatched_snapshot,),
            (self.snapshot, self.snapshot),
            *((snapshot,) for snapshot in changed_snapshots),
        ):
            with self.subTest(snapshots=snapshots):
                self.sources.snapshots = snapshots
                with self.assertRaises(ReadinessError):
                    self.plan(_fields(_finding()))

    def test_direct_cell_does_not_prove_another_target_using_the_same_source_cell(
        self,
    ) -> None:
        mapping = _mapping(
            self.selection,
            ScalarFieldMapping("name", _KEY),
            ScalarFieldMapping(
                "formula", _KEY, transform=ScalarTransformPolicy(formula="upper(value)")
            ),
        )
        evidence, requests = self.plan(
            _fields(_finding(), _finding(target="formula")),
            mappings=(mapping,),
        )
        self.assertEqual(requests[0].cells, ((1, _KEY),))
        self.assertEqual(requests[0].targets, ((1, _KEY, "name"),))
        self.assertEqual(
            evidence.findings[1].source_value.state, OriginalValueState.UNAVAILABLE
        )

    def test_physical_source_row_can_exceed_accepted_row_count(self) -> None:
        _, requests = self.plan(_fields(_finding(row=19)))
        self.assertEqual(requests[0].snapshot.row_count, 3)
        self.assertEqual(requests[0].cells, ((19, _KEY),))
        self.assertEqual(requests[0].targets, ((19, _KEY, "name"),))

    def test_invalid_source_row_is_rejected_without_reading_source_evidence(
        self,
    ) -> None:
        for row in (0, -1, True):
            with self.subTest(row=row):
                with self.assertRaises(ReadinessError):
                    self.plan(_fields(_finding(row=row)))
        self.assertEqual(self.sources.calls, [])

    def test_target_model_or_ambiguous_provider_does_not_establish_direct_lineage(
        self,
    ) -> None:
        mapping = _mapping(self.selection)
        for mappings in (
            (replace(mapping, target_model="product.product"),),
            (mapping, mapping),
        ):
            with self.subTest(mappings=mappings):
                _, requests = self.plan(_fields(_finding()), mappings=mappings)
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])


if __name__ == "__main__":
    unittest.main()
