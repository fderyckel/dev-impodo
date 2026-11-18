"""Export saved join and group inputs without reopening or recomposing sources."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from openpyxl import load_workbook

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.artifacts.review_original_values import (
    populate_workbook_original_values,
)
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotCandidateWriter,
    source_snapshot_schema,
)
from impodo.application.workspace.review.original_values import (
    plan_workbook_original_values,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preflight.fields_set_aside import field_set_aside_evidence
from impodo.domain.preflight.reports import ReviewWorkbookEvidence
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.preparation.staging_contracts import CanonicalRow
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.structural import (
    AggregateOperation,
    AggregateSpec,
    ExactJoinRule,
    GroupAggregateRule,
    GroupKey,
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
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    mapping_source_selection,
)
from tests.integration.artifacts import test_relationship_original_values as shared

_HASH = shared._HASH
_NOW = shared._NOW
_OWNER = shared._OWNER
_IDS = {"left": "dataset:" + "1" * 24, "right": "dataset:" + "2" * 24}


def _source(name, values):
    dataset = SourceDataset(
        _IDS[name],
        name.title(),
        FileSourceBinding(name, "csv", _HASH, _HASH, "utf-8", ",", 1),
        len(values),
        tuple(
            SourceDatasetColumn(index, key.title(), f"{name}:{key}", "string")
            for index, key in enumerate(("key", "scope", "value"), 1)
        ),
    )
    table = SourceTable(
        name.title(),
        Path(f"removed-{name}.csv"),
        ("Key", "Scope", "Value"),
        tuple(
            SourceRow(
                index, {"Key": f"row-{index}", "Scope": "company", "Value": value}
            )
            for index, value in enumerate(values, 2)
        ),
        _HASH,
    )
    return dataset, table


class _Sources:
    def __init__(self, selection, snapshots):
        self.selection = selection
        self.snapshots = snapshots
        self.calls = []

    def get_source_selection(self, workspace_id):
        self.calls.append("selection")
        return self.selection

    def get_current_source_snapshots(self, workspace_id):
        self.calls.append("snapshots")
        return self.snapshots


def _evaluate(directory, inputs, rule, mapping):
    physical = SourceSelection(
        str(uuid4()),
        1,
        _OWNER,
        _NOW,
        "reviewer",
        tuple(item[0] for item in inputs),
        _HASH,
    )
    plan = DerivedEntityPlan(
        str(uuid4()),
        1,
        _OWNER,
        _HASH,
        (rule,),
        _NOW,
        "reviewer",
        dataset_uses=tuple(
            SourceDatasetUsePolicy(item.dataset_id, SourceDatasetUse.PREPARATION_ONLY)
            for item in physical.datasets
        ),
    )
    selection = mapping_source_selection(physical, plan)
    evaluated = evaluate_browser_mapping(
        workspace_id=_OWNER,
        definition=MappingDefinition(
            "structural_originals", selection.content_hash, _HASH, (mapping,)
        ),
        physical_selection=physical,
        effective_selection=selection,
        plan=plan,
        loaded_tables={dataset.dataset_id: table for dataset, table in inputs},
    )
    rows = tuple(
        CanonicalRow.from_dict(row.to_portable_dict())
        for row in evaluated.canonical_run.rows
    )
    fields = field_set_aside_evidence(
        evaluated.plan,
        rows,
        evaluated.source_field_labels,
        {
            (row.dataset, target): keys
            for row in rows
            for target, keys in row.lineage.field_sources.items()
        },
        column_exclusions={},
    )
    artifacts = LocalArtifactStore(directory / "artifacts")
    snapshots = []
    for dataset, table in inputs:
        schema = source_snapshot_schema(dataset)
        with artifacts.prepare_source_snapshot(_OWNER) as workspace:
            writer = SourceSnapshotCandidateWriter(workspace, schema, batch_rows=1_000)
            writer.append_source_rows(table.rows)
            candidate = writer.finalize()
            snapshot = SourceSnapshot.create(
                data_version_id=_OWNER,
                dataset_id=dataset.dataset_id,
                dataset_name=dataset.name,
                source=dataset.source,
                physical_selection_hash=_HASH,
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
            snapshots.append(snapshot)
    sources = _Sources(physical, tuple(snapshots))
    with (
        patch(
            "impodo.domain.staging.evaluator.evaluate_browser_mapping",
            side_effect=AssertionError("Replayed mapping during export planning"),
        ),
        patch(
            "impodo.domain.structural.execute_structural_rules",
            side_effect=AssertionError("Replayed composition during export planning"),
        ),
    ):
        fields, requests = plan_workbook_original_values(
            fields=fields,
            selection=selection,
            physical_selection_hash=_HASH,
            mappings=(mapping,),
            sources=sources,
            workspace_id=_OWNER,
            restricted=False,
            workbook_lineage={row.row_id: row.lineage for row in rows},
            derived_plan=plan,
        )
    evidence = ReviewWorkbookEvidence(
        frozen_input_hash=_HASH,
        records=(),
        dataset_labels={},
        target_model_labels={},
        target_field_labels={},
        normalization_content_hash="",
        cell_effects=(),
        target_field_required={},
        fields_set_aside=fields,
        original_source_requests=requests,
    )
    return evidence, artifacts, sources, rows


class StructuralOriginalExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _export(self, evidence, artifacts, name, expected):
        with (
            patch.object(
                artifacts,
                "materialize_source",
                side_effect=AssertionError("Reopened removed upload"),
            ),
            patch(
                "impodo.domain.staging.evaluator.evaluate_browser_mapping",
                side_effect=AssertionError("Replayed mapping evaluation"),
            ),
            patch(
                "impodo.domain.structural.execute_structural_rules",
                side_effect=AssertionError("Replayed composition"),
            ),
            patch.object(
                artifacts,
                "materialize_source_snapshot",
                wraps=artifacts.materialize_source_snapshot,
            ) as reads,
        ):
            hydrated = populate_workbook_original_values(evidence, artifacts)
        self.assertEqual(reads.call_count, len(evidence.original_source_requests))
        self.assertEqual(
            tuple(
                (
                    item.coordinate.dataset_id,
                    item.coordinate.source_row,
                    item.coordinate.source_key,
                    item.value.text,
                )
                for finding in hydrated.fields_set_aside.findings
                for item in finding.source_inputs
            ),
            expected,
        )
        for major in (19, 20):
            for mode in ("local", "remote"):
                with self.subTest(name=name, major=major, mode=mode):
                    path = shared._workbook(
                        self.root / f"{name}-{major}-{mode}",
                        hydrated,
                        major=major,
                        mode=mode,
                    )
                    with closing(load_workbook(path)) as workbook:
                        sheet = workbook["Fields set aside"]
                        self.assertEqual(
                            tuple(
                                sheet.cell(row, 3).value
                                for row in range(7, 7 + len(expected))
                            ),
                            tuple(item[1] for item in expected),
                        )
                        self.assertEqual(
                            tuple(
                                sheet.cell(row, 2).value
                                for row in range(7, 7 + len(expected))
                            ),
                            tuple(
                                "Left" if item[0] == _IDS["left"] else "Right"
                                for item in expected
                            ),
                        )
                        self.assertEqual(
                            tuple(
                                sheet.cell(row, 5).value
                                for row in range(7, 7 + len(expected))
                            ),
                            tuple(item[3] or None for item in expected),
                        )
                        self.assertEqual(
                            sheet["A4"].value,
                            f"{len(expected)} identified source cells",
                        )

    def test_scoped_join_exports_each_physical_input_from_its_own_snapshot(self):
        rule = ExactJoinRule(
            str(uuid4()),
            "joined",
            _IDS["left"],
            _IDS["right"],
            (JoinKey("left:key", "right:key"),),
            tuple(
                StructuralOutputColumn(key, key, "string")
                for key in ("joined:key", "joined:value", "joined:scope")
            ),
            (
                StructuralProjection("joined:key", _IDS["left"], "left:key"),
                StructuralProjection("joined:value", _IDS["left"], "left:value"),
                StructuralProjection("joined:scope", _IDS["right"], "right:value"),
            ),
        )
        mapping = DatasetMapping(
            dataset_id=structural_dataset_id(rule),
            target_model="res.partner",
            source_identity_column_keys=("joined:key",),
            target_identity=(IdentityComponentMapping(("joined:key",), ("ref",)),),
            fields=(ScalarFieldMapping("name", "joined:key"),),
            relationships=(
                RelationshipMapping(
                    "category_id",
                    "many2one",
                    ("joined:value", "joined:scope"),
                    RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model="res.partner.category",
                        key_mappings=(ReferenceKeyMapping("joined:value", "name"),),
                        scope_mappings=(
                            ReferenceKeyMapping("joined:scope", "company_id"),
                        ),
                    ),
                    required=True,
                ),
            ),
        )
        evidence, artifacts, sources, rows = _evaluate(
            self.root / "join",
            (_source("left", ("",)), _source("right", ("0007",))),
            rule,
            mapping,
        )
        self.assertEqual(
            rows[0].lineage.physical_sources, {_IDS["left"]: (2,), _IDS["right"]: (2,)}
        )
        self.assertEqual(sources.calls, ["selection", "snapshots"])
        self._export(
            evidence,
            artifacts,
            "join",
            (
                (_IDS["left"], 2, "left:value", ""),
                (_IDS["right"], 2, "right:value", "0007"),
            ),
        )

    def test_grouped_failed_scalar_exports_exact_original_member_values(self):
        rule = GroupAggregateRule(
            str(uuid4()),
            "grouped",
            _IDS["left"],
            (
                StructuralOutputColumn("group:key", "Key", "string"),
                StructuralOutputColumn("group:total", "Total", "decimal"),
            ),
            (GroupKey("group:key", "left:scope"),),
            (AggregateSpec("group:total", AggregateOperation.SUM, "left:value"),),
        )
        mapping = DatasetMapping(
            dataset_id=structural_dataset_id(rule),
            target_model="x.measure",
            source_identity_column_keys=("group:key",),
            target_identity=(IdentityComponentMapping(("group:key",), ("x_code",)),),
            fields=(
                ScalarFieldMapping(
                    "x_amount",
                    "group:total",
                    value_type="decimal",
                    transform=ScalarTransformPolicy(formula="value / 0"),
                ),
            ),
        )
        evidence, artifacts, sources, rows = _evaluate(
            self.root / "group",
            (_source("left", ("001.50", "002.50")),),
            rule,
            mapping,
        )
        self.assertEqual(rows[0].lineage.physical_sources, {_IDS["left"]: (2, 3)})
        self.assertEqual(sources.calls, ["selection", "snapshots"])
        self._export(
            evidence,
            artifacts,
            "group",
            (
                (_IDS["left"], 2, "left:value", "001.50"),
                (_IDS["left"], 3, "left:value", "002.50"),
            ),
        )
