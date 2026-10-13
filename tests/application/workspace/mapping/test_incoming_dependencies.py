from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from impodo.application.workspace.mapping.incoming_dependencies import (
    repair_unambiguous_incoming_dependencies,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


class IncomingDependencyRepairTests(unittest.TestCase):
    def test_ungoverned_operation_lookup_uses_the_unique_incoming_dataset(
        self,
    ) -> None:
        selection, schema, governance, datasets = _bom_mapping()

        result = repair_unambiguous_incoming_dependencies(
            datasets,
            selection,
            schema,
            governance,
        )

        line = next(
            item for item in result.datasets if item.dataset_id == "lines"
        )
        operation = next(
            item
            for item in line.relationships
            if item.target_field == "operation_id"
        )
        self.assertEqual(
            operation.source_column_keys,
            ("line.item", "line.operation_number"),
        )
        self.assertIs(operation.resolver.origin, ResolverOrigin.DATASET)
        self.assertEqual(operation.resolver.dataset_id, "operations")
        self.assertIsNone(operation.resolver.model)
        self.assertEqual(operation.resolver.key_mappings, ())
        self.assertEqual(
            tuple(item.target_model for item in result.datasets),
            ("mrp.routing.workcenter", "mrp.bom.line"),
        )
        self.assertEqual(
            result.repairs[0].target_field,
            "operation_id",
        )

        repeated = repair_unambiguous_incoming_dependencies(
            result.datasets,
            selection,
            schema,
            governance,
        )
        self.assertEqual(repeated.datasets, result.datasets)
        self.assertEqual(repeated.repairs, ())

        product = next(
            item
            for item in line.relationships
            if item.target_field == "product_id"
        )
        self.assertIs(
            product.resolver.origin,
            ResolverOrigin.TARGET_CATALOG,
        )

    def test_governed_target_lookup_is_never_rewritten(self) -> None:
        selection, schema, governance, datasets = _bom_mapping()
        line = datasets[1]
        operation = line.relationships[0]
        governed_operation = RelationshipMapping(
            target_field=operation.target_field,
            kind=operation.kind,
            source_column_keys=("line.operation", "line.bom"),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="mrp.routing.workcenter",
                key_mappings=(
                    ReferenceKeyMapping("line.operation", "name"),
                ),
                scope_mappings=(
                    ReferenceKeyMapping("line.bom", "bom_id"),
                ),
            ),
        )
        datasets = (
            datasets[0],
            DatasetMapping(
                dataset_id=line.dataset_id,
                target_model=line.target_model,
                source_identity_column_keys=line.source_identity_column_keys,
                target_identity=line.target_identity,
                target_scope=line.target_scope,
                relationships=(governed_operation, *line.relationships[1:]),
            ),
        )

        result = repair_unambiguous_incoming_dependencies(
            datasets,
            selection,
            schema,
            governance,
        )

        self.assertEqual(result.datasets, datasets)
        self.assertEqual(result.repairs, ())

    def test_two_operation_datasets_leave_the_target_lookup_unchanged(
        self,
    ) -> None:
        selection, schema, governance, datasets = _bom_mapping()
        second_source = replace(
            selection.datasets[0],
            dataset_id="operations:second",
        )
        selection = SourceSelection(
            selection_id=selection.selection_id,
            version=selection.version,
            data_version_id=selection.data_version_id,
            created_at=selection.created_at,
            created_by=selection.created_by,
            datasets=(*selection.datasets, second_source),
            content_hash=selection.content_hash,
        )
        second_mapping = DatasetMapping(
            dataset_id=second_source.dataset_id,
            target_model=datasets[0].target_model,
            source_identity_column_keys=(
                "operation.item",
                "operation.operation_number",
            ),
        )

        result = repair_unambiguous_incoming_dependencies(
            (*datasets, second_mapping),
            selection,
            schema,
            governance,
        )

        self.assertEqual(result.datasets, (*datasets, second_mapping))
        self.assertEqual(result.repairs, ())

    def test_ambiguous_owner_column_names_leave_lookup_unchanged(self) -> None:
        selection, schema, governance, datasets = _bom_mapping()
        line_source = selection.datasets[1]
        duplicate = SourceDatasetColumn(
            7,
            "Item Id",
            "line.item.duplicate",
            "string",
        )
        selection = SourceSelection(
            selection_id=selection.selection_id,
            version=selection.version,
            data_version_id=selection.data_version_id,
            created_at=selection.created_at,
            created_by=selection.created_by,
            datasets=(
                selection.datasets[0],
                SourceDataset(
                    dataset_id=line_source.dataset_id,
                    name=line_source.name,
                    source=line_source.source,
                    row_count=line_source.row_count,
                    columns=(*line_source.columns, duplicate),
                ),
            ),
            content_hash=selection.content_hash,
        )

        result = repair_unambiguous_incoming_dependencies(
            datasets,
            selection,
            schema,
            governance,
        )

        self.assertEqual(result.datasets, datasets)
        self.assertEqual(result.repairs, ())

    def test_validate_only_relationship_is_not_rewritten(self) -> None:
        selection, schema, governance, datasets = _bom_mapping()
        line = datasets[1]
        relationships = (
            replace(line.relationships[0], validate_only=True),
            *line.relationships[1:],
        )
        datasets = (
            datasets[0],
            DatasetMapping(
                dataset_id=line.dataset_id,
                target_model=line.target_model,
                source_identity_column_keys=line.source_identity_column_keys,
                target_identity=line.target_identity,
                target_scope=line.target_scope,
                relationships=relationships,
            ),
        )

        result = repair_unambiguous_incoming_dependencies(
            datasets,
            selection,
            schema,
            governance,
        )

        self.assertEqual(result.datasets, datasets)
        self.assertEqual(result.repairs, ())


def _bom_mapping():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    binding = FileSourceBinding(
        file_id="bom-files",
        table_key="xlsx",
        source_sha256="a" * 64,
        catalog_hash="sha256:" + "b" * 64,
        encoding=None,
        delimiter=None,
        header_row=1,
    )
    selection = SourceSelection(
        selection_id="selection:bom",
        version=1,
        data_version_id="version:bom",
        created_at=now,
        created_by="Data manager",
        datasets=(
            SourceDataset(
                dataset_id="operations",
                name="plw_bom_operations",
                source=binding,
                row_count=10,
                columns=(
                    SourceDatasetColumn(1, "Item Id", "operation.item", "string"),
                    SourceDatasetColumn(
                        2,
                        "Opr Num",
                        "operation.operation_number",
                        "string",
                    ),
                    SourceDatasetColumn(
                        3,
                        "Opr Id",
                        "operation.operation",
                        "string",
                    ),
                    SourceDatasetColumn(4, "BOMId", "operation.bom", "string"),
                ),
            ),
            SourceDataset(
                dataset_id="lines",
                name="plw_bom_lines",
                source=binding,
                row_count=10,
                columns=(
                    SourceDatasetColumn(1, "ItemId", "line.item", "string"),
                    SourceDatasetColumn(
                        2,
                        "OprNum",
                        "line.operation_number",
                        "string",
                    ),
                    SourceDatasetColumn(3, "Opr Id", "line.operation", "string"),
                    SourceDatasetColumn(4, "BOMId", "line.bom", "string"),
                    SourceDatasetColumn(5, "LineNum", "line.sequence", "integer"),
                ),
            ),
        ),
        content_hash="sha256:selection",
    )

    def field(name, field_type="char", *, relation=None):
        return SchemaField(
            name=name,
            label=name,
            type=field_type,
            required=False,
            readonly=False,
            relation=relation,
            relation_field=None,
            selection=(),
        )

    schema = OdooSchemaCatalog(
        workspace_id="workspace:bom",
        policy_hash="sha256:policy",
        captured_at=now,
        captured_by="Data manager",
        connection_mode="LOCAL",
        database="odoo20",
        odoo_version="20.0",
        models=(
            SchemaModel(
                "mrp.routing.workcenter",
                "Operations",
                (field("name"), field("bom_id", "many2one", relation="mrp.bom")),
            ),
            SchemaModel(
                "mrp.bom.line",
                "BOM Lines",
                (
                    field("sequence", "integer"),
                    field("bom_id", "many2one", relation="mrp.bom"),
                    field(
                        "operation_id",
                        "many2one",
                        relation="mrp.routing.workcenter",
                    ),
                    field(
                        "product_id",
                        "many2one",
                        relation="product.product",
                    ),
                ),
            ),
        ),
        content_hash="sha256:schema",
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "1" * 64,
        read_principal_hash="sha256:" + "2" * 64,
        read_permission_hash="sha256:" + "3" * 64,
        read_context_hash="sha256:" + "4" * 64,
        connection_target_hash="sha256:" + "5" * 64,
    )
    governance = SchemaGovernance(
        governance_id="governance:bom",
        version=1,
        workspace_id=schema.workspace_id,
        catalog_hash=schema.content_hash,
        permitted_models=tuple(item.name for item in schema.models),
        business_keys=(
            BusinessKeyDefinition(
                key_id="operation-within-bom",
                model="mrp.routing.workcenter",
                key_fields=("name",),
                scope_fields=("bom_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                key_id="line-within-bom",
                model="mrp.bom.line",
                key_fields=("sequence",),
                scope_fields=("bom_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=now,
        recorded_by="Data manager",
    )
    operation_mapping = DatasetMapping(
        dataset_id="operations",
        target_model="mrp.routing.workcenter",
        source_identity_column_keys=(
            "operation.item",
            "operation.operation_number",
        ),
        target_identity=(
            IdentityComponentMapping(("operation.operation",), ("name",)),
        ),
        target_scope=(
            IdentityComponentMapping(("operation.bom",), ("bom_id",)),
        ),
    )
    line_mapping = DatasetMapping(
        dataset_id="lines",
        target_model="mrp.bom.line",
        source_identity_column_keys=("line.bom", "line.item"),
        target_identity=(
            IdentityComponentMapping(("line.sequence",), ("sequence",)),
        ),
        target_scope=(IdentityComponentMapping(("line.bom",), ("bom_id",)),),
        relationships=(
            RelationshipMapping(
                target_field="operation_id",
                kind="many2one",
                source_column_keys=("line.operation",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG,
                    model="mrp.routing.workcenter",
                    key_mappings=(
                        ReferenceKeyMapping("line.operation", "name"),
                    ),
                ),
                categorical_policy=(
                    CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
                ),
            ),
            RelationshipMapping(
                target_field="product_id",
                kind="many2one",
                source_column_keys=("line.item",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG,
                    model="product.product",
                    key_mappings=(
                        ReferenceKeyMapping("line.item", "default_code"),
                    ),
                ),
                categorical_policy=(
                    CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
                ),
            ),
        ),
    )
    return selection, schema, governance, (operation_mapping, line_mapping)


if __name__ == "__main__":
    unittest.main()
