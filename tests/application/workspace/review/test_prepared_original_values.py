"""Prove prepared relationship originals without evaluating rules or changing lineage."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.mapping.reference_preparation import ReferencePreparation
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
from impodo.domain.preflight.relationship_inputs import relationship_original_inputs
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
_ANCHOR = "column:1:" + "1" * 12
_EXTRA = "column:2:" + "2" * 12
_SCOPE = "column:3:" + "3" * 12
_SYNTHETIC = "generated:hierarchy-path"
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
                tuple(
                    SourceDatasetColumn(index, name, key, "string")
                    for index, name, key in (
                        (1, "Authored anchor", _ANCHOR),
                        (2, "Prepared code", _EXTRA),
                        (3, "Company", _SCOPE),
                    )
                ),
            ),
        ),
        _HASH,
    )


def _relationship(
    preparation: ReferencePreparation, *, scoped: bool = False
) -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id",
        "many2one",
        (_ANCHOR, _SCOPE) if scoped else (_ANCHOR,),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping(_ANCHOR, "ref", preparation),),
            scope_mappings=(ReferenceKeyMapping(_SCOPE, "company_id"),)
            if scoped
            else (),
        ),
    )


def _finding(keys: tuple[str, ...], *, comparison: bool = False) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.INVALID,
        "Customers",
        19,
        keys,
        "parent_id",
        "Saved reference failure.",
        comparison_issue=comparison,
    )


def _fields(*findings: FieldSetAside) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=tuple(findings),
        source_labels={},
        source_keys={},
        null_omissions={},
        upsert_datasets=frozenset(),
        target_models={"Customers": "res.partner"},
        coverage_gaps=(),
    )


class _Sources:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection = selection
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
        self.calls: list[str] = []

    def get_mapping_source_selection(self, workspace_id: str) -> SourceSelection | None:
        raise AssertionError("Frozen mapping selection must not be reloaded")

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append("snapshots")
        return self.snapshots


class PreparedOriginalValuePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _selection()
        self.sources = _Sources(self.selection)
        self.preparation = ReferencePreparation(
            formula='concat("prefix/", column_2)', bindings=(("column_2", _EXTRA),)
        )
        self.relationship = _relationship(self.preparation)

    def plan(
        self, relationship: RelationshipMapping, finding: FieldSetAside, **changes
    ):
        options = {
            "fields": _fields(finding),
            "selection": self.selection,
            "physical_selection_hash": _HASH,
            "mappings": (
                DatasetMapping(
                    self.selection.datasets[0].dataset_id,
                    "res.partner",
                    relationships=(relationship,),
                ),
            ),
            "sources": self.sources,
            "workspace_id": "workspace-1",
            "restricted": False,
        }
        options.update(changes)
        return plan_workbook_original_values(**options)

    def assert_cohort(self, fields, requests, keys: tuple[str, ...]) -> None:
        finding = fields.findings[0]
        self.assertEqual(
            tuple(item.coordinate.source_key for item in finding.source_inputs), keys
        )
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for item in finding.source_inputs
            )
        )
        self.assertEqual(
            requests[0].cells, _cells(tuple((19, key) for key in sorted(keys)))
        )
        self.assertEqual(
            requests[0].targets,
            tuple(
                _target(19, key, "parent_id", keys, source_keys=finding.source_keys)
                for key in sorted(keys)
            ),
        )

    def test_unused_authored_anchor_is_not_a_contributing_original_cell(self) -> None:
        proof = relationship_original_inputs(self.relationship)
        self.assertEqual(
            (proof.saved_keys, proof.input_keys), ((_ANCHOR, _EXTRA), (_EXTRA,))
        )
        fields, requests = self.plan(self.relationship, _finding(proof.saved_keys))
        self.assertEqual(fields.findings[0].source_keys, (_ANCHOR, _EXTRA))
        self.assert_cohort(fields, requests, (_EXTRA,))
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_full_saved_envelope_must_match_before_actual_dependencies_can_be_used(
        self,
    ) -> None:
        for saved in ((_EXTRA,), (_EXTRA, _ANCHOR), (_ANCHOR, _EXTRA, _SCOPE)):
            with self.subTest(saved=saved):
                fields, requests = self.plan(self.relationship, _finding(saved))
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_prepared_original_projection_never_evaluates_or_recreates_the_reference(
        self,
    ) -> None:
        with (
            patch(
                "impodo.domain.mapping.reference_preparation.prepare_reference_value"
            ) as prepare,
            patch(
                "impodo.domain.mapping.reference_preparation.evaluate_reference_formula"
            ) as evaluate,
        ):
            fields, requests = self.plan(self.relationship, _finding((_ANCHOR, _EXTRA)))
        prepare.assert_not_called()
        evaluate.assert_not_called()
        self.assert_cohort(fields, requests, (_EXTRA,))

    def test_unused_synthetic_anchor_is_allowed_when_every_actual_input_is_physical(
        self,
    ) -> None:
        dataset = self.selection.datasets[0]
        effective = replace(
            self.selection,
            datasets=(
                replace(
                    dataset,
                    columns=(
                        *dataset.columns,
                        SourceDatasetColumn(
                            4, "Generated anchor", _SYNTHETIC, "string"
                        ),
                    ),
                ),
            ),
        )
        relationship = replace(
            self.relationship,
            source_column_keys=(_SYNTHETIC,),
            resolver=replace(
                self.relationship.resolver,
                key_mappings=(
                    ReferenceKeyMapping(_SYNTHETIC, "ref", self.preparation),
                ),
            ),
        )
        fields, requests = self.plan(
            relationship, _finding((_SYNTHETIC, _EXTRA)), selection=effective
        )
        self.assert_cohort(fields, requests, (_EXTRA,))

    def test_mixed_physical_and_synthetic_actual_dependencies_reject_the_complete_cohort(
        self,
    ) -> None:
        dataset = self.selection.datasets[0]
        effective = replace(
            self.selection,
            datasets=(
                replace(
                    dataset,
                    columns=(
                        *dataset.columns,
                        SourceDatasetColumn(4, "Generated input", _SYNTHETIC, "string"),
                    ),
                ),
            ),
        )
        relationship = _relationship(
            ReferencePreparation(
                formula="concat(value, column_4)", bindings=(("column_4", _SYNTHETIC),)
            )
        )
        fields, requests = self.plan(
            relationship, _finding((_ANCHOR, _SYNTHETIC)), selection=effective
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, ["selection"])

    def test_even_an_unused_saved_anchor_must_exist_in_effective_schema(self) -> None:
        relationship = replace(
            self.relationship,
            source_column_keys=("missing-anchor",),
            resolver=replace(
                self.relationship.resolver,
                key_mappings=(
                    ReferenceKeyMapping("missing-anchor", "ref", self.preparation),
                ),
            ),
        )
        with self.assertRaises(ReadinessError):
            self.plan(relationship, _finding(("missing-anchor", _EXTRA)))
        self.assertEqual(self.sources.calls, [])

    def test_replanning_preserves_saved_envelope_and_discards_earlier_originals(
        self,
    ) -> None:
        finding = replace(
            _finding((_ANCHOR, _EXTRA)),
            source_inputs=(
                OriginalSourceInput(
                    _coord(19, _EXTRA),
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Previous")
                    ),
                ),
            ),
        )
        fields, requests = self.plan(self.relationship, finding)
        self.assertEqual(fields.findings[0].source_keys, finding.source_keys)
        self.assert_cohort(fields, requests, (_EXTRA,))
        fields_again, requests_again = self.plan(self.relationship, fields.findings[0])
        self.assertEqual(fields_again, fields)
        self.assertEqual(requests_again, requests)

    def test_restricted_inputs_are_cleared_before_rules_or_source_evidence_are_opened(
        self,
    ) -> None:
        finding = replace(
            _finding((_ANCHOR, _EXTRA)),
            source_inputs=(
                OriginalSourceInput(
                    _coord(19, _EXTRA),
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Private")
                    ),
                ),
            ),
        )
        fields, requests = self.plan(self.relationship, finding, restricted=True)
        self.assertTrue(fields.source_values_restricted)
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_missing_snapshot_keeps_known_actual_geometry_without_inventing_an_anchor(
        self,
    ) -> None:
        self.sources.snapshots = ()
        fields, requests = self.plan(self.relationship, _finding((_ANCHOR, _EXTRA)))
        self.assertEqual(requests, ())
        self.assertEqual(
            tuple(
                item.coordinate.source_key for item in fields.findings[0].source_inputs
            ),
            (_EXTRA,),
        )
        self.assertEqual(fields.findings[0].source_keys, (_ANCHOR, _EXTRA))
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])


if __name__ == "__main__":
    unittest.main()
