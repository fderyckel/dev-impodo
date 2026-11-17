"""Prove complete scoped relationship cohorts before planning original cells."""

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
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
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
    OriginalSourceTarget,
    OriginalSourceValue,
    OriginalValueState,
)
from impodo.domain.source_binding import DerivedSourceBinding, FileSourceBinding
from impodo.domain.source_snapshot import EncodedSourceCell, SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

_HASH = "sha256:" + "a" * 64
_KEY = "column:1:" + "1" * 12
_SCOPE = "column:2:" + "2" * 12
_THIRD = "column:3:" + "3" * 12
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
                        (1, "Code", _KEY),
                        (2, "Company", _SCOPE),
                        (3, "Country", _THIRD),
                    )
                ),
            ),
        ),
        _HASH,
    )


def _snapshot(selection: SourceSelection) -> SourceSnapshot:
    dataset = selection.datasets[0]
    return SourceSnapshot.create(
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
    )


def _relationship() -> RelationshipMapping:
    return RelationshipMapping(
        "parent_id",
        "many2one",
        (_KEY, _SCOPE),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model="res.partner",
            key_mappings=(ReferenceKeyMapping(_KEY, "ref"),),
            scope_mappings=(ReferenceKeyMapping(_SCOPE, "company_id"),),
        ),
    )


def _finding(
    *, target: str = "parent_id", keys: tuple[str, ...] = (_KEY, _SCOPE)
) -> FieldSetAside:
    return FieldSetAside(
        FieldSetAsideReason.MISSING,
        "Customers",
        19,
        keys,
        target,
        "A required reference component is blank.",
    )


def _fields(*findings: FieldSetAside) -> FieldSetAsideEvidence:
    return FieldSetAsideEvidence(
        findings=tuple(findings),
        source_labels={},
        source_keys={("Customers", "parent_id"): (_KEY, _SCOPE)},
        null_omissions={},
        upsert_datasets=frozenset(),
        target_models={"Customers": "res.partner"},
        coverage_gaps=(),
    )


class _Sources:
    def __init__(self, selection: SourceSelection) -> None:
        self.selection: SourceSelection | None = selection
        self.snapshots = (_snapshot(selection),)
        self.calls: list[str] = []

    def get_mapping_source_selection(self, workspace_id: str) -> SourceSelection | None:
        raise AssertionError("The effective frozen selection must not be reloaded")

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None:
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(
        self, workspace_id: str
    ) -> tuple[SourceSnapshot, ...]:
        self.calls.append("snapshots")
        return self.snapshots


class ScopedOriginalValuePlanTests(unittest.TestCase):
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

    def assert_proven(self, finding: FieldSetAside, keys: tuple[str, ...]) -> None:
        self.assertEqual(tuple(item.source_key for item in finding.source_inputs), keys)
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for item in finding.source_inputs
            )
        )

    def test_saved_scoped_preparation_finding_proves_both_ordered_inputs(self) -> None:
        fields, requests = self.plan(_fields(_finding()))
        self.assert_proven(fields.findings[0], (_KEY, _SCOPE))
        self.assertEqual(requests[0].cells, ((19, _KEY), (19, _SCOPE)))
        self.assertEqual(
            requests[0].targets,
            (
                OriginalSourceTarget(19, _KEY, "parent_id", (_KEY, _SCOPE)),
                OriginalSourceTarget(19, _SCOPE, "parent_id", (_KEY, _SCOPE)),
            ),
        )
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_each_saved_comparison_outcome_uses_the_same_complete_scoped_cohort(
        self,
    ) -> None:
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
                self.assert_proven(planned.findings[0], (_KEY, _SCOPE))
                self.assertEqual(
                    {target.input_keys for target in requests[0].targets},
                    {(_KEY, _SCOPE)},
                )

    def test_target_then_dataset_and_multiple_scope_components_remain_direct(
        self,
    ) -> None:
        resolver = replace(
            self.relationship.resolver,
            origin=ResolverOrigin.TARGET_THEN_DATASET,
            dataset_id="incoming-parent",
            scope_mappings=(
                *self.relationship.resolver.scope_mappings,
                ReferenceKeyMapping(_THIRD, "country_id"),
            ),
        )
        relationship = replace(
            self.relationship,
            source_column_keys=(_KEY, _SCOPE, _THIRD),
            resolver=resolver,
        )
        fields, requests = self.plan(
            _fields(_finding(keys=relationship.source_column_keys)),
            mappings=(replace(self.mapping, relationships=(relationship,)),),
        )
        self.assert_proven(fields.findings[0], relationship.source_column_keys)
        self.assertEqual(len(requests[0].cells), 3)
        self.assertTrue(
            all(
                target.input_keys == relationship.source_column_keys
                for target in requests[0].targets
            )
        )

    def test_missing_snapshot_retains_complete_unavailable_physical_geometry(
        self,
    ) -> None:
        self.sources.snapshots = ()
        fields, requests = self.plan(_fields(_finding()))
        self.assertEqual(requests, ())
        self.assert_proven(fields.findings[0], (_KEY, _SCOPE))
        self.assertIn("snapshots are missing", fields.coverage_gaps[-1])
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_missing_selection_evidence_never_preserves_earlier_inputs(self) -> None:
        captured = replace(
            _finding(),
            source_inputs=tuple(
                OriginalSourceInput(
                    key,
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Private value")
                    ),
                )
                for key in (_KEY, _SCOPE)
            ),
        )
        for changes in ({"selection": None}, {"physical_selection_hash": ""}):
            with self.subTest(changes=changes):
                fields, requests = self.plan(_fields(captured), **changes)
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_mixed_physical_generated_scope_keeps_the_entire_group_unproven(
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
                        SourceDatasetColumn(4, "Selected path", _SYNTHETIC, "string"),
                    ),
                ),
            ),
        )
        relationship = replace(
            self.relationship,
            source_column_keys=(_KEY, _SYNTHETIC),
            resolver=replace(
                self.relationship.resolver,
                scope_mappings=(ReferenceKeyMapping(_SYNTHETIC, "company_id"),),
            ),
        )
        fields, requests = self.plan(
            _fields(_finding(keys=relationship.source_column_keys)),
            selection=effective,
            mappings=(replace(self.mapping, relationships=(relationship,)),),
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, ["selection"])

    def test_rejected_mixed_group_does_not_block_independent_direct_fields(
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
                        SourceDatasetColumn(4, "Selected path", _SYNTHETIC, "string"),
                    ),
                ),
            ),
        )
        mixed = replace(
            self.relationship,
            source_column_keys=(_KEY, _SYNTHETIC),
            resolver=replace(
                self.relationship.resolver,
                scope_mappings=(ReferenceKeyMapping(_SYNTHETIC, "company_id"),),
            ),
        )
        mapping = replace(
            self.mapping,
            fields=(ScalarFieldMapping("name", _KEY),),
            relationships=(mixed,),
        )
        fields, requests = self.plan(
            _fields(
                _finding(keys=mixed.source_column_keys),
                _finding(target="name", keys=(_KEY,)),
            ),
            selection=effective,
            mappings=(mapping,),
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assert_proven(fields.findings[1], (_KEY,))
        self.assertEqual(
            requests[0].targets, (OriginalSourceTarget(19, _KEY, "name", (_KEY,)),)
        )
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_shared_key_does_not_mix_target_scopes_or_duplicate_physical_reads(
        self,
    ) -> None:
        second = replace(
            self.relationship,
            target_field="other_parent_id",
            source_column_keys=(_KEY, _THIRD),
            resolver=replace(
                self.relationship.resolver,
                scope_mappings=(ReferenceKeyMapping(_THIRD, "country_id"),),
            ),
        )
        mapping = replace(self.mapping, relationships=(self.relationship, second))
        fields, requests = self.plan(
            _fields(
                _finding(),
                _finding(target=second.target_field, keys=second.source_column_keys),
            ),
            mappings=(mapping,),
        )
        self.assertEqual(len(fields.findings), 2)
        self.assertEqual(len(requests[0].cells), 3)
        self.assertEqual(len(requests[0].targets), 4)
        self.assertEqual(
            {
                target.input_keys
                for target in requests[0].targets
                if target.target_field == "parent_id"
            },
            {(_KEY, _SCOPE)},
        )
        self.assertEqual(
            {
                target.input_keys
                for target in requests[0].targets
                if target.target_field == second.target_field
            },
            {(_KEY, _THIRD)},
        )
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_incomplete_reordered_or_forged_finding_cohorts_do_not_read_sources(
        self,
    ) -> None:
        for keys in ((_KEY,), (_SCOPE, _KEY), (_KEY, _SCOPE, _THIRD), (_KEY, _KEY)):
            with self.subTest(keys=keys):
                fields, requests = self.plan(_fields(_finding(keys=keys)))
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_prepared_translated_projected_or_invalid_scoped_providers_stay_unavailable(
        self,
    ) -> None:
        resolver = self.relationship.resolver
        relationships = (
            replace(self.relationship, kind="many2many"),
            replace(
                self.relationship,
                resolver=replace(
                    resolver,
                    origin=ResolverOrigin.DATASET,
                    dataset_id="incoming-parent",
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(resolver, dataset_projection_field="display_name"),
            ),
            replace(
                self.relationship,
                resolver=replace(
                    resolver, value_mappings=(ValueMapping("West", "WEST"),)
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(
                    resolver,
                    key_mappings=(
                        ReferenceKeyMapping(
                            _KEY, "ref", ReferencePreparation(formula="upper(value)")
                        ),
                    ),
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(
                    resolver,
                    scope_mappings=(
                        ReferenceKeyMapping(
                            _SCOPE,
                            "company_id",
                            ReferencePreparation(
                                parts=(ReferencePart(source_column_key=_SCOPE),)
                            ),
                        ),
                    ),
                ),
            ),
            replace(
                self.relationship,
                source_column_keys=(_KEY, _KEY),
                resolver=replace(
                    resolver, scope_mappings=(ReferenceKeyMapping(_KEY, "company_id"),)
                ),
            ),
            replace(
                self.relationship,
                resolver=replace(
                    resolver, scope_mappings=(ReferenceKeyMapping(_SCOPE, "ref"),)
                ),
            ),
        )
        for relationship in relationships:
            with self.subTest(relationship=relationship):
                fields, requests = self.plan(
                    _fields(_finding(keys=relationship.source_column_keys)),
                    mappings=(replace(self.mapping, relationships=(relationship,)),),
                )
                self.assertEqual(fields.findings[0].source_inputs, ())
                self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_scoped_choice_expansion_is_not_mistaken_for_multiple_physical_inputs(
        self,
    ) -> None:
        relationship = replace(
            self.relationship,
            source_column_keys=(_KEY,),
            resolver=replace(
                self.relationship.resolver,
                scope_mappings=(ReferenceKeyMapping(_KEY, "company_id"),),
                value_mappings=(ValueMapping("West", '["WEST","Company"]'),),
            ),
        )
        fields, requests = self.plan(
            _fields(_finding(keys=(_KEY,))),
            mappings=(replace(self.mapping, relationships=(relationship,)),),
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_derived_effective_sources_do_not_authorize_scoped_physical_cells(
        self,
    ) -> None:
        derived = replace(
            self.selection.datasets[0],
            source=DerivedSourceBinding(_HASH, ("input-dataset",), _HASH),
        )
        fields, requests = self.plan(
            _fields(_finding()), selection=replace(self.selection, datasets=(derived,))
        )
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_restricted_replanning_clears_values_and_geometry_before_any_read(
        self,
    ) -> None:
        captured = replace(
            _finding(),
            source_inputs=tuple(
                OriginalSourceInput(
                    key,
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Private value")
                    ),
                )
                for key in (_KEY, _SCOPE)
            ),
        )
        fields, requests = self.plan(_fields(captured), restricted=True)
        self.assertTrue(fields.source_values_restricted)
        self.assertEqual(fields.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_public_replanning_proves_geometry_again_and_drops_all_cached_values(
        self,
    ) -> None:
        captured = replace(
            _finding(),
            source_inputs=tuple(
                OriginalSourceInput(
                    key,
                    OriginalSourceValue.from_cell(
                        EncodedSourceCell.from_python("Previous value")
                    ),
                )
                for key in (_KEY, _SCOPE)
            ),
        )
        fields, requests = self.plan(
            replace(_fields(captured), source_values_restricted=True)
        )
        self.assertFalse(fields.source_values_restricted)
        self.assert_proven(fields.findings[0], (_KEY, _SCOPE))
        self.assertEqual(len(requests[0].targets), 2)
        self.assertEqual(captured.source_inputs[0].value.text, "Previous value")

    def test_many_findings_share_one_complete_bounded_capture(self) -> None:
        fields, requests = self.plan(_fields(*(_finding() for _ in range(100))))
        self.assertEqual(len(fields.findings), 100)
        self.assertTrue(
            all(len(finding.source_inputs) == 2 for finding in fields.findings)
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0].cells), 2)
        self.assertEqual(len(requests[0].targets), 2)
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])

    def test_contradictory_physical_schema_is_rejected_before_snapshot_reads(
        self,
    ) -> None:
        dataset = self.selection.datasets[0]
        self.sources.selection = replace(
            self.selection,
            datasets=(
                replace(
                    dataset,
                    columns=(
                        replace(dataset.columns[0], source_name="Changed"),
                        *dataset.columns[1:],
                    ),
                ),
            ),
        )
        with self.assertRaises(ReadinessError):
            self.plan(_fields(_finding()))
        self.assertEqual(self.sources.calls, ["selection"])


if __name__ == "__main__":
    unittest.main()
