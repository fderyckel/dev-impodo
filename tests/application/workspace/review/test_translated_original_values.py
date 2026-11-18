"""Plan one physical original for scoped reviewed choice outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase

from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.preflight.comparison_fields import (
    COMPARISON_RELATIONSHIP_CODES,
    comparison_relationship_findings,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceValue,
    OriginalValueState,
)
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
_CHOICE = "column:1:" + "1" * 12
_SYNTHETIC = "generated:choice"
_NOW = datetime(2026, 11, 17, tzinfo=UTC)


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
                (SourceDatasetColumn(1, "Choice", _CHOICE, "string"),),
            ),
        ),
        _HASH,
    )


def _relationship() -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id",
        "many2one",
        (_CHOICE,),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping(_CHOICE, "ref"),),
            scope_mappings=(ReferenceKeyMapping(_CHOICE, "company_id"),),
            value_mappings=(ValueMapping("West", '["WEST","Company A"]'),),
        ),
    )


def _finding(
    *, target: str = "parent_id", keys: tuple[str, ...] = (_CHOICE,)
) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.MISSING,
        "Customers",
        19,
        keys,
        target,
        "Saved blocked relationship outcome.",
    )


def _fields(*findings: FieldSetAside) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        tuple(findings),
        {},
        {("Customers", "parent_id"): (_CHOICE,)},
        {},
        frozenset(),
        {"Customers": "res.partner"},
        (),
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
        raise AssertionError("Frozen effective selection must not be reloaded")

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append("snapshots")
        return self.snapshots


class TranslatedOriginalValuePlanTests(TestCase):
    def setUp(self) -> None:
        self.selection = _selection()
        self.sources = _Sources(self.selection)
        self.relationship = _relationship()
        self.mapping = DatasetMapping(
            self.selection.datasets[0].dataset_id,
            "res.partner",
            relationships=(self.relationship,),
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

    def test_saved_preparation_outcome_authorizes_one_choice_and_one_target(
        self,
    ) -> None:
        fields, requests = self.plan(_fields(_finding()))
        self.assertEqual(
            tuple(
                item.coordinate.source_key for item in fields.findings[0].source_inputs
            ),
            (_CHOICE,),
        )
        self.assertEqual(requests[0].cells, _cells(((19, _CHOICE),)))
        self.assertEqual(
            requests[0].targets,
            (_target(19, _CHOICE, "parent_id", (_CHOICE,)),),
        )
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_each_saved_comparison_outcome_has_one_original_input(self) -> None:
        for code in sorted(COMPARISON_RELATIONSHIP_CODES):
            with self.subTest(code=code):
                fields = _fields()
                findings = comparison_relationship_findings(
                    (
                        {
                            "classification": "BLOCKED",
                            "dataset": "Customers",
                            "source_row": 19,
                            "issues": [
                                {
                                    "code": code,
                                    "field": "parent_id",
                                    "message": f"Saved {code} outcome.",
                                }
                            ],
                        },
                    ),
                    fields,
                )
                planned, requests = self.plan(replace(fields, findings=findings))
                self.assertTrue(planned.findings[0].comparison_issue)
                self.assertEqual(len(planned.findings[0].source_inputs), 1)
                self.assertEqual(len(requests[0].cells), 1)

    def test_distinct_target_outcomes_share_one_bulk_physical_cell_read(self) -> None:
        other = replace(self.relationship, target_field="other_parent_id")
        mapping = replace(self.mapping, relationships=(self.relationship, other))
        fields, requests = self.plan(
            _fields(_finding(), _finding(target=other.target_field)),
            mappings=(mapping,),
        )
        self.assertEqual(len(fields.findings), 2)
        self.assertEqual(len(requests[0].cells), 1)
        self.assertEqual(len(requests[0].targets), 2)
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_invalid_selected_tuple_metadata_returns_before_source_reads(self) -> None:
        relation = replace(
            self.relationship,
            resolver=replace(
                self.relationship.resolver,
                value_mappings=(ValueMapping("West", '["WEST",null]'),),
            ),
        )
        fields, requests = self.plan(
            _fields(_finding()),
            mappings=(replace(self.mapping, relationships=(relation,)),),
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_repeated_internal_slots_are_not_accepted_as_saved_canonical_lineage(
        self,
    ) -> None:
        fields, requests = self.plan(_fields(_finding(keys=(_CHOICE, _CHOICE))))
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_restricted_originals_are_removed_before_any_source_read(self) -> None:
        finding = replace(
            _finding(),
            source_inputs=(
                OriginalSourceInput(
                    _coord(19, _CHOICE),
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Private")
                    ),
                ),
            ),
        )
        fields, requests = self.plan(_fields(finding), restricted=True)
        self.assertTrue(fields.source_values_restricted)
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_missing_snapshot_preserves_one_proven_unavailable_choice_input(
        self,
    ) -> None:
        self.sources.snapshots = ()
        fields, requests = self.plan(_fields(_finding()))
        self.assertEqual(requests, ())
        self.assertEqual(len(fields.findings[0].source_inputs), 1)
        self.assertEqual(
            fields.findings[0].source_inputs[0].value.state,
            OriginalValueState.UNAVAILABLE,
        )

    def test_generated_choice_never_becomes_a_physical_source_cell(self) -> None:
        dataset = self.selection.datasets[0]
        effective = replace(
            self.selection,
            datasets=(
                replace(
                    dataset,
                    columns=(
                        *dataset.columns,
                        SourceDatasetColumn(
                            2, "Generated choice", _SYNTHETIC, "string"
                        ),
                    ),
                ),
            ),
        )
        relation = replace(
            self.relationship,
            source_column_keys=(_SYNTHETIC,),
            resolver=replace(
                self.relationship.resolver,
                key_mappings=(ReferenceKeyMapping(_SYNTHETIC, "ref"),),
                scope_mappings=(ReferenceKeyMapping(_SYNTHETIC, "company_id"),),
            ),
        )
        fields, requests = self.plan(
            _fields(_finding(keys=(_SYNTHETIC,))),
            selection=effective,
            mappings=(replace(self.mapping, relationships=(relation,)),),
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, ["selection"])

    def test_blank_choices_do_not_create_new_omission_findings(self) -> None:
        relation = replace(self.relationship, blank_source_values=("OMIT",))
        fields, requests = self.plan(
            _fields(), mappings=(replace(self.mapping, relationships=(relation,)),)
        )
        self.assertEqual(fields.findings, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])
