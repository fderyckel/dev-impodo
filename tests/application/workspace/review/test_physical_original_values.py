"""Plan saved actual scalar dependencies across complete physical cohorts."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from impodo.application.data_version.source_snapshots import source_snapshot_schema
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ScalarConcatenation,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
)
from impodo.domain.preflight.original_values import OriginalValueState
from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    StructuralOutputColumn,
    StructuralProjection,
    structural_dataset_id,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    mapping_source_selection,
)

_HASH = "sha256:" + "a" * 64
_LEFT = "dataset:" + "1" * 24
_RIGHT = "dataset:" + "2" * 24
_IDS = {"left": _LEFT, "right": _RIGHT}
_NOW = datetime(2026, 11, 18, tzinfo=UTC)


class _Sources:
    def __init__(self, selection):
        self.selection = selection
        self.calls = []
        self.snapshots = tuple(
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
            )
            for dataset in selection.datasets
        )

    def get_source_selection(self, workspace_id):
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(self, workspace_id):
        self.calls.append("snapshots")
        return self.snapshots


def _physical():
    datasets = tuple(
        SourceDataset(
            dataset_id=_IDS[name],
            name=name.title(),
            source=FileSourceBinding(
                name, f"{name}.csv", _HASH, _HASH, "utf-8", ",", 1
            ),
            row_count=20,
            columns=tuple(
                SourceDatasetColumn(index, key.title(), f"{name}:{key}", "string")
                for index, key in enumerate(("key", "value"), 1)
            ),
        )
        for name in ("left", "right")
    )
    return SourceSelection("selection", 1, "version", _NOW, "Reviewer", datasets, _HASH)


def _lineage(selection, dataset, keys, actual, *, rows=None, plan=None):
    rows = rows or {dataset.dataset_id: (4,)}
    primary = next(iter(rows))
    return CanonicalLineage(
        selection.content_hash,
        _HASH,
        _HASH,
        _HASH,
        plan.content_hash if plan else None,
        dataset.name,
        1,
        primary,
        rows[primary],
        {"name": keys},
        rows,
        {"name": actual},
    )


def _finding(dataset, keys, trace="trace-1"):
    return FieldSetAside(
        FieldSetAsideReason.INVALID,
        dataset.name,
        1,
        keys,
        "name",
        "Value omitted.",
        source_trace_id=trace,
    )


def _fields(*findings):
    return FieldSetAsideEvidence(
        tuple(findings),
        {},
        {},
        {},
        frozenset(),
        {finding.dataset: "res.partner" for finding in findings},
        (),
    )


def _cells(finding):
    return tuple(
        (
            item.coordinate.dataset_id,
            item.coordinate.source_row,
            item.coordinate.source_key,
        )
        for item in finding.source_inputs
    )


class PhysicalOriginalValuePlanTests(TestCase):
    def setUp(self):
        self.physical = _physical()
        self.selection = self.physical
        self.sources = _Sources(self.physical)
        self.dataset = self.selection.datasets[0]
        self.plan = None

    def run_plan(self, fields, mappings, lineage):
        return plan_workbook_original_values(
            fields=fields,
            selection=self.selection,
            physical_selection_hash=self.physical.content_hash,
            mappings=mappings,
            sources=self.sources,
            workspace_id="workspace",
            restricted=False,
            workbook_lineage=lineage,
            derived_plan=self.plan,
        )

    def test_actual_multi_input_readset_uses_saved_physical_row_and_labels(self):
        keys = ("left:key", "left:value")
        mapping = DatasetMapping(
            _LEFT,
            "res.partner",
            fields=(
                ScalarFieldMapping(
                    "name",
                    value_source=ScalarValueSource.CONCATENATE,
                    concatenation=ScalarConcatenation(keys),
                ),
            ),
        )
        finding = _finding(self.dataset, keys)
        lineage = _lineage(self.selection, self.dataset, keys, keys)
        evidence, requests = self.run_plan(
            _fields(finding), (mapping,), {"trace-1": lineage}
        )
        self.assertEqual(
            _cells(evidence.findings[0]), ((_LEFT, 4, keys[0]), (_LEFT, 4, keys[1]))
        )
        self.assertEqual(evidence.source_labels[("Left", keys[1])], "Value")
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])
        self.assertTrue(
            all(
                target.occurrence.source_trace_id == "trace-1"
                for target in requests[0].targets
            )
        )

    def test_recorded_conditional_readset_can_exclude_an_unused_saved_anchor(self):
        keys = ("left:key", "left:value")
        mapping = DatasetMapping(
            _LEFT, "res.partner", fields=(ScalarFieldMapping("name", keys[0]),)
        )
        lineage = _lineage(self.selection, self.dataset, keys, (keys[1],))
        evidence, requests = self.run_plan(
            _fields(_finding(self.dataset, keys)), (mapping,), {"trace-1": lineage}
        )
        self.assertEqual(_cells(evidence.findings[0]), ((_LEFT, 4, keys[1]),))
        self.assertEqual(len(requests[0].cells), 1)

    def test_recorded_constant_is_known_without_repository_or_snapshot_reads(self):
        mapping = DatasetMapping(
            _LEFT,
            "res.partner",
            fields=(
                ScalarFieldMapping(
                    "name",
                    value_source=ScalarValueSource.CONSTANT,
                    literal_value="invalid",
                ),
            ),
        )
        lineage = _lineage(self.selection, self.dataset, (), ())
        fields = _fields(_finding(self.dataset, ()))
        evidence, requests = self.run_plan(fields, (mapping,), {"trace-1": lineage})
        self.assertTrue(evidence.findings[0].source_inputs_known)
        self.assertEqual(evidence.findings[0].source_inputs, ())
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])
        with self.assertRaises(ReadinessError):
            self.run_plan(
                fields,
                (mapping,),
                {
                    "trace-1": replace(
                        lineage, source_selection_hash="sha256:" + "b" * 64
                    )
                },
            )
        self.assertEqual(self.sources.calls, [])

    def test_trace_mismatch_and_unknown_actual_column_fail_before_metadata_reads(self):
        keys = ("left:key",)
        mapping = DatasetMapping(
            _LEFT, "res.partner", fields=(ScalarFieldMapping("name", keys[0]),)
        )
        lineage = _lineage(self.selection, self.dataset, keys, keys)
        for lineages in (
            {},
            {"trace-1": replace(lineage, dataset="Wrong")},
            {"trace-1": replace(lineage, field_input_keys={"name": ("missing",)})},
        ):
            with self.subTest(lineages=lineages), self.assertRaises(ReadinessError):
                self.run_plan(
                    _fields(_finding(self.dataset, keys)), (mapping,), lineages
                )
        self.assertEqual(self.sources.calls, [])

    def test_reference_comparison_issue_never_authorizes_a_scalar_provider(self):
        keys = ("left:key",)
        mapping = DatasetMapping(
            _LEFT, "res.partner", fields=(ScalarFieldMapping("name", keys[0]),)
        )
        finding = replace(_finding(self.dataset, keys), comparison_issue=True)
        lineage = _lineage(self.selection, self.dataset, keys, keys)
        evidence, requests = self.run_plan(
            _fields(finding), (mapping,), {"trace-1": lineage}
        )
        self.assertFalse(evidence.findings[0].source_inputs_known)
        self.assertEqual(requests, ())
        self.assertEqual(self.sources.calls, [])

    def test_join_cohort_missing_one_snapshot_never_emits_partial_authorization(self):
        rule = ExactJoinRule(
            str(uuid4()),
            "joined",
            _LEFT,
            _RIGHT,
            (JoinKey("left:key", "right:key"),),
            tuple(
                StructuralOutputColumn(f"joined:{name}", name.title(), "string")
                for name in ("left", "right")
            ),
            tuple(
                StructuralProjection(f"joined:{name}", _IDS[name], f"{name}:value")
                for name in ("left", "right")
            ),
        )
        self.plan = DerivedEntityPlan(
            str(uuid4()),
            1,
            "workspace",
            self.physical.content_hash,
            (rule,),
            _NOW,
            "Reviewer",
        )
        self.selection = mapping_source_selection(self.physical, self.plan)
        joined = next(
            dataset
            for dataset in self.selection.datasets
            if dataset.dataset_id == structural_dataset_id(rule)
        )
        keys = ("joined:left", "joined:right")
        join_mapping = DatasetMapping(
            joined.dataset_id,
            "res.partner",
            fields=(
                ScalarFieldMapping(
                    "name",
                    value_source=ScalarValueSource.CONCATENATE,
                    concatenation=ScalarConcatenation(keys),
                ),
            ),
        )
        direct_mapping = DatasetMapping(
            _LEFT, "res.partner", fields=(ScalarFieldMapping("name", "left:value"),)
        )
        lineages = {
            "joined-trace": _lineage(
                self.selection,
                joined,
                keys,
                keys,
                rows={_LEFT: (9,), _RIGHT: (3,)},
                plan=self.plan,
            ),
            "direct-trace": _lineage(
                self.selection,
                self.dataset,
                ("left:value",),
                ("left:value",),
                rows={_LEFT: (8,)},
                plan=self.plan,
            ),
        }
        fields = _fields(
            _finding(joined, keys, "joined-trace"),
            _finding(self.dataset, ("left:value",), "direct-trace"),
        )
        self.sources.snapshots = self.sources.snapshots[:1]
        evidence, requests = self.run_plan(
            fields, (join_mapping, direct_mapping), lineages
        )
        self.assertEqual(
            _cells(evidence.findings[0]),
            ((_LEFT, 9, "left:value"), (_RIGHT, 3, "right:value")),
        )
        self.assertTrue(
            all(
                item.value.state is OriginalValueState.UNAVAILABLE
                for item in evidence.findings[0].source_inputs
            )
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(tuple(cell.source_row for cell in requests[0].cells), (8,))
        self.assertEqual(
            requests[0].targets[0].occurrence.source_trace_id, "direct-trace"
        )
        self.assertIn("snapshots are missing", evidence.coverage_gaps[-1])
        self.assertEqual(self.sources.calls, ["selection", "snapshots"])
        self.sources.calls.clear()
        self.sources.snapshots = _Sources(self.physical).snapshots
        _, requests = self.run_plan(
            _fields(fields.findings[0]), (join_mapping,), lineages
        )
        self.assertEqual(len(requests), 2)
        cohorts = [
            target.input_coordinates
            for request in requests
            for target in request.targets
        ]
        self.assertEqual(cohorts[0], cohorts[1])
