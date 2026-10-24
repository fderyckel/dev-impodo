"""Qualify generic source composition for separate BoM-family grains."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.matching_order import (
    DatasetOrderEdge,
    order_dataset_dependency_components,
)
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.relationship_dependencies import (
    DependencySource,
    DependencyStrength,
    extract_dataset_dependency_edges,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.structural import (
    ExactJoinRule,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    execute_structural_rules,
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


def _hash(number: int) -> str:
    return "sha256:" + format(number, "064x")


def _dataset(
    dataset_id: str,
    name: str,
    columns: tuple[tuple[str, str, str], ...],
    *,
    row_count: int,
    marker: int,
) -> SourceDataset:
    return SourceDataset(
        dataset_id=dataset_id,
        name=name,
        source=FileSourceBinding(
            file_id=f"file-{dataset_id}",
            table_key=f"{dataset_id}.csv",
            source_sha256=_hash(marker),
            catalog_hash=_hash(marker + 100),
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=row_count,
        columns=tuple(
            SourceDatasetColumn(index, source_name, stable_key, value_type)
            for index, (source_name, stable_key, value_type) in enumerate(
                columns,
                start=1,
            )
        ),
    )


def _table(
    dataset_id: str,
    headers: tuple[str, ...],
    rows: tuple[dict[str, object], ...],
    *,
    marker: int,
) -> SourceTable:
    return SourceTable(
        dataset=dataset_id,
        path=Path(f"{dataset_id}.csv"),
        headers=headers,
        rows=tuple(
            SourceRow(index, values) for index, values in enumerate(rows, start=1)
        ),
        content_hash=_hash(marker),
    )


def _relationship_resolver(
    *,
    header_dataset_id: str,
    company_column: str,
    bom_column: str,
) -> RelationshipResolver:
    return RelationshipResolver(
        origin=ResolverOrigin.DATASET,
        dataset_id=header_dataset_id,
        key_mappings=(
            ReferenceKeyMapping(company_column, "x_company_code"),
            ReferenceKeyMapping(bom_column, "code"),
        ),
    )


class BomSourceCompositionQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.header_core = _dataset(
            "header-core",
            "BoM header core",
            (
                ("Company", "header:company", "string"),
                ("BoM", "header:bom", "string"),
                ("Product", "header:product", "string"),
            ),
            row_count=2,
            marker=1,
        )
        self.header_details = _dataset(
            "header-details",
            "BoM header details",
            (
                ("Company", "header-detail:company", "string"),
                ("BoM", "header-detail:bom", "string"),
                ("Description", "header-detail:description", "string"),
            ),
            row_count=2,
            marker=2,
        )
        self.line_core = _dataset(
            "line-core",
            "BoM line core",
            (
                ("Company", "line:company", "string"),
                ("BoM", "line:bom", "string"),
                ("Line", "line:number", "integer"),
                ("Component", "line:component", "string"),
                ("Quantity", "line:quantity", "decimal"),
            ),
            row_count=3,
            marker=3,
        )
        self.line_details = _dataset(
            "line-details",
            "BoM line details",
            (
                ("Company", "line-detail:company", "string"),
                ("BoM", "line-detail:bom", "string"),
                ("Line", "line-detail:number", "integer"),
                ("Scrap percent", "line-detail:scrap", "decimal"),
            ),
            row_count=3,
            marker=4,
        )
        self.operations = _dataset(
            "operations",
            "BoM operations",
            (
                ("Company", "operation:company", "string"),
                ("BoM", "operation:bom", "string"),
                ("Operation", "operation:number", "integer"),
                ("Work center", "operation:work-center", "string"),
                ("Duration", "operation:duration", "decimal"),
            ),
            row_count=4,
            marker=5,
        )
        self.selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            created_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
            created_by="Data manager",
            datasets=(
                self.header_core,
                self.header_details,
                self.line_core,
                self.line_details,
                self.operations,
            ),
            content_hash=_hash(20),
        )
        self.header_rule = ExactJoinRule(
            rule_id="00000000-0000-0000-0000-000000000101",
            output_dataset_name="bom_headers",
            left_dataset_id=self.header_core.dataset_id,
            right_dataset_id=self.header_details.dataset_id,
            keys=(
                JoinKey("header:company", "header-detail:company"),
                JoinKey("header:bom", "header-detail:bom"),
            ),
            output_columns=(
                StructuralOutputColumn("header:company", "Company", "string"),
                StructuralOutputColumn("header:bom", "BoM", "string"),
                StructuralOutputColumn("header:product", "Product", "string"),
                StructuralOutputColumn(
                    "header:description",
                    "Description",
                    "string",
                ),
            ),
            projections=(
                StructuralProjection(
                    "header:company",
                    self.header_core.dataset_id,
                    "header:company",
                ),
                StructuralProjection(
                    "header:bom",
                    self.header_core.dataset_id,
                    "header:bom",
                ),
                StructuralProjection(
                    "header:product",
                    self.header_core.dataset_id,
                    "header:product",
                ),
                StructuralProjection(
                    "header:description",
                    self.header_details.dataset_id,
                    "header-detail:description",
                ),
            ),
            kind=JoinKind.INNER,
        )
        self.line_rule = ExactJoinRule(
            rule_id="00000000-0000-0000-0000-000000000102",
            output_dataset_name="bom_lines",
            left_dataset_id=self.line_core.dataset_id,
            right_dataset_id=self.line_details.dataset_id,
            keys=(
                JoinKey("line:company", "line-detail:company"),
                JoinKey("line:bom", "line-detail:bom"),
                JoinKey("line:number", "line-detail:number", "integer"),
            ),
            output_columns=(
                StructuralOutputColumn("line:company", "Company", "string"),
                StructuralOutputColumn("line:bom", "BoM", "string"),
                StructuralOutputColumn("line:number", "Line", "integer"),
                StructuralOutputColumn(
                    "line:component",
                    "Component",
                    "string",
                ),
                StructuralOutputColumn("line:quantity", "Quantity", "decimal"),
                StructuralOutputColumn(
                    "line:scrap",
                    "Scrap percent",
                    "decimal",
                ),
            ),
            projections=(
                StructuralProjection(
                    "line:company",
                    self.line_core.dataset_id,
                    "line:company",
                ),
                StructuralProjection(
                    "line:bom",
                    self.line_core.dataset_id,
                    "line:bom",
                ),
                StructuralProjection(
                    "line:number",
                    self.line_core.dataset_id,
                    "line:number",
                ),
                StructuralProjection(
                    "line:component",
                    self.line_core.dataset_id,
                    "line:component",
                ),
                StructuralProjection(
                    "line:quantity",
                    self.line_core.dataset_id,
                    "line:quantity",
                ),
                StructuralProjection(
                    "line:scrap",
                    self.line_details.dataset_id,
                    "line-detail:scrap",
                ),
            ),
            kind=JoinKind.INNER,
        )
        self.plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=str(uuid4()),
            source_selection_hash=self.selection.content_hash,
            rules=(self.header_rule, self.line_rule),
            updated_at=datetime(2026, 10, 23, 9, 5, tzinfo=UTC),
            updated_by="Data manager",
            dataset_uses=(
                SourceDatasetUsePolicy(
                    self.header_core.dataset_id,
                    SourceDatasetUse.PREPARATION_ONLY,
                ),
                SourceDatasetUsePolicy(
                    self.header_details.dataset_id,
                    SourceDatasetUse.PREPARATION_ONLY,
                ),
                SourceDatasetUsePolicy(
                    self.line_core.dataset_id,
                    SourceDatasetUse.PREPARATION_ONLY,
                ),
                SourceDatasetUsePolicy(
                    self.line_details.dataset_id,
                    SourceDatasetUse.PREPARATION_ONLY,
                ),
                SourceDatasetUsePolicy(
                    self.operations.dataset_id,
                    SourceDatasetUse.MAP_DIRECTLY,
                ),
            ),
        )
        self.tables = {
            self.header_core.dataset_id: _table(
                self.header_core.dataset_id,
                ("Company", "BoM", "Product"),
                (
                    {"Company": "BE", "BoM": "B-100", "Product": "P-100"},
                    {"Company": "LU", "BoM": "B-100", "Product": "P-200"},
                ),
                marker=1,
            ),
            self.header_details.dataset_id: _table(
                self.header_details.dataset_id,
                ("Company", "BoM", "Description"),
                (
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Description": "Belgian cap",
                    },
                    {
                        "Company": "LU",
                        "BoM": "B-100",
                        "Description": "Luxembourg cap",
                    },
                ),
                marker=2,
            ),
            self.line_core.dataset_id: _table(
                self.line_core.dataset_id,
                ("Company", "BoM", "Line", "Component", "Quantity"),
                (
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Line": 10,
                        "Component": "C-10",
                        "Quantity": "2.0",
                    },
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Line": 20,
                        "Component": "C-20",
                        "Quantity": "1.0",
                    },
                    {
                        "Company": "LU",
                        "BoM": "B-100",
                        "Line": 10,
                        "Component": "C-30",
                        "Quantity": "4.0",
                    },
                ),
                marker=3,
            ),
            self.line_details.dataset_id: _table(
                self.line_details.dataset_id,
                ("Company", "BoM", "Line", "Scrap percent"),
                (
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Line": 10,
                        "Scrap percent": "1.5",
                    },
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Line": 20,
                        "Scrap percent": "0.0",
                    },
                    {
                        "Company": "LU",
                        "BoM": "B-100",
                        "Line": 10,
                        "Scrap percent": "2.0",
                    },
                ),
                marker=4,
            ),
            self.operations.dataset_id: _table(
                self.operations.dataset_id,
                ("Company", "BoM", "Operation", "Work center", "Duration"),
                (
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Operation": 10,
                        "Work center": "WC-10",
                        "Duration": "1.0",
                    },
                    {
                        "Company": "BE",
                        "BoM": "B-100",
                        "Operation": 20,
                        "Work center": "WC-20",
                        "Duration": "2.0",
                    },
                    {
                        "Company": "LU",
                        "BoM": "B-100",
                        "Operation": 10,
                        "Work center": "WC-10",
                        "Duration": "1.5",
                    },
                    {
                        "Company": "LU",
                        "BoM": "B-100",
                        "Operation": 20,
                        "Work center": "WC-30",
                        "Duration": "2.5",
                    },
                ),
                marker=5,
            ),
        }

    def test_header_and_partial_line_joins_keep_three_distinct_grains(self) -> None:
        execution = execute_structural_rules(
            selection=self.selection,
            loaded_tables=self.tables,
            rules=self.plan.rules,
        )
        outputs = {item.dataset.name: item for item in execution.outputs}
        mapping_selection = mapping_source_selection(self.selection, self.plan)

        self.assertEqual(
            {item.name: item.row_count for item in mapping_selection.datasets},
            {"BoM operations": 4, "bom_headers": 2, "bom_lines": 3},
        )
        self.assertEqual(len(outputs["bom_headers"].table.rows), 2)
        self.assertEqual(len(outputs["bom_lines"].table.rows), 3)
        self.assertEqual(len(self.tables[self.operations.dataset_id].rows), 4)
        self.assertEqual(
            outputs["bom_headers"].lineage[1],
            {"header-core": (1,), "header-details": (1,)},
        )
        self.assertEqual(
            outputs["bom_lines"].lineage[1],
            {"line-core": (1,), "line-details": (1,)},
        )
        self.assertEqual(outputs["bom_headers"].reconciliation.output_rows, 2)
        self.assertEqual(outputs["bom_lines"].reconciliation.output_rows, 3)
        self.assertNotIn("Work center", outputs["bom_headers"].table.headers)
        self.assertNotIn("Component", outputs["bom_headers"].table.headers)

    def test_line_and_operation_dependencies_keep_header_first(self) -> None:
        header_dataset_id = structural_dataset_id(self.header_rule)
        line_dataset_id = structural_dataset_id(self.line_rule)
        line_header = _relationship_resolver(
            header_dataset_id=header_dataset_id,
            company_column="line:company",
            bom_column="line:bom",
        )
        operation_header = _relationship_resolver(
            header_dataset_id=header_dataset_id,
            company_column="operation:company",
            bom_column="operation:bom",
        )
        mappings = (
            DatasetMapping(
                dataset_id=line_dataset_id,
                target_model="mrp.bom.line",
                target_identity=(
                    IdentityComponentMapping(
                        ("line:company", "line:bom"),
                        ("bom_id",),
                        resolver=line_header,
                    ),
                    IdentityComponentMapping(
                        ("line:number",),
                        ("sequence",),
                        value_type="integer",
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        "bom_id",
                        "many2one",
                        ("line:company", "line:bom"),
                        line_header,
                        required=True,
                        required_on_create=True,
                    ),
                ),
            ),
            DatasetMapping(
                dataset_id=self.operations.dataset_id,
                target_model="mrp.routing.workcenter",
                target_identity=(
                    IdentityComponentMapping(
                        ("operation:company", "operation:bom"),
                        ("bom_id",),
                        resolver=operation_header,
                    ),
                    IdentityComponentMapping(
                        ("operation:number",),
                        ("sequence",),
                        value_type="integer",
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        "bom_id",
                        "many2one",
                        ("operation:company", "operation:bom"),
                        operation_header,
                        required=True,
                        required_on_create=True,
                    ),
                ),
            ),
            DatasetMapping(
                dataset_id=header_dataset_id,
                target_model="mrp.bom",
                target_identity=(
                    IdentityComponentMapping(
                        ("header:company",),
                        ("x_company_code",),
                    ),
                    IdentityComponentMapping(
                        ("header:bom",),
                        ("code",),
                    ),
                ),
            ),
        )

        dependencies = extract_dataset_dependency_edges(mappings)
        order = order_dataset_dependency_components(
            tuple(item.dataset_id for item in mappings),
            tuple(
                DatasetOrderEdge(
                    owner_dataset=item.owner_dataset,
                    dependency_dataset=item.dependency_dataset,
                )
                for item in dependencies
            ),
        )

        self.assertEqual(
            {
                (item.owner_dataset, item.dependency_dataset)
                for item in dependencies
            },
            {
                (line_dataset_id, header_dataset_id),
                (self.operations.dataset_id, header_dataset_id),
            },
        )
        self.assertEqual(
            {item.source for item in dependencies},
            {DependencySource.TARGET_IDENTITY, DependencySource.RELATIONSHIP},
        )
        self.assertEqual(
            {item.strength for item in dependencies},
            {DependencyStrength.HARD},
        )
        self.assertEqual(order.ordered_dataset_ids[0], header_dataset_id)
        self.assertEqual(
            set(order.ordered_dataset_ids[1:]),
            {line_dataset_id, self.operations.dataset_id},
        )

    def test_composition_rules_do_not_contain_odoo_model_branches(self) -> None:
        payload = (self.header_rule.to_dict(), self.line_rule.to_dict())

        self.assertNotIn("target_model", str(payload))
        self.assertNotIn("mrp.bom", str(payload))
        self.assertNotIn("res.partner", str(payload))


if __name__ == "__main__":
    unittest.main()
