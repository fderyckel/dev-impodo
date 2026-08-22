"""Qualify representative Product, related BOM, and stock Recipe semantics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest
from uuid import uuid4

from impodo.access import LOCAL_ACTOR
from impodo.application.recipe_authoring_service import RecipeAuthoringService
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.derived_entities import DerivedEntityPlan, RelatedDatasetRule
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
from impodo.domain.mapping.contracts import (
    BusinessControlDefinition,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.recipe_parameters import (
    RecipeParameterDefinition,
    RecipeParameterDefinitions,
)
from impodo.domain.recipe_applications import RecipeControlValues
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.source_binding import DerivedSourceBinding
from impodo.quality import default_quality_ruleset
from impodo.recipes import DataVersion, DataVersionPurpose, DataVersionState, Recipe
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)

from tests.recipe_compiler_helpers import Evidence, file_binding


def _field(
    name: str,
    field_type: str = "char",
    *,
    relation: str | None = None,
    required: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=name.replace("_", " ").title(),
        type=field_type,
        required=required,
        readonly=False,
        relation=relation,
        relation_field=None,
        selection=(),
    )


def _dataset(
    name: str, columns: tuple[tuple[str, str], ...], marker: str
) -> SourceDataset:
    return SourceDataset(
        dataset_id=f"physical-{name.casefold().replace(' ', '-')}-{uuid4()}",
        name=name,
        source=file_binding(marker),
        row_count=3,
        columns=tuple(
            SourceDatasetColumn(index, source_name, stable_key, candidate_type)
            for index, (source_name, candidate_type) in enumerate(columns, start=1)
            for stable_key in (f"physical:{source_name}",)
        ),
    )


def _column(dataset: SourceDataset, name: str) -> str:
    return next(item.stable_key for item in dataset.columns if item.source_name == name)


def _key(model: str, *fields: str) -> BusinessKeyDefinition:
    return BusinessKeyDefinition(
        key_id=f"{model}:{':'.join(fields)}",
        model=model,
        key_fields=tuple(fields),
        status=BusinessKeyStatus.CONFIRMED,
    )


def _publish(
    *,
    base_selection: SourceSelection,
    mapping_selection: SourceSelection,
    mappings: tuple[DatasetMapping, ...],
    models: tuple[SchemaModel, ...],
    business_keys: tuple[BusinessKeyDefinition, ...],
    preparation: DerivedEntityPlan | None = None,
    parameters: tuple[RecipeParameterDefinition, ...] = (),
    mapping_contract_version: int | None = None,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    project_id = base_selection.project_id
    schema_hash = "sha256:" + "b" * 64
    schema = OdooSchemaCatalog(
        project_id=project_id,
        policy_hash="sha256:" + "1" * 64,
        captured_at=now,
        captured_by="Data manager",
        connection_mode="REMOTE",
        database="authoring_test",
        odoo_version="19.0",
        models=models,
        content_hash=schema_hash,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "2" * 64,
        read_principal_hash="sha256:" + "3" * 64,
        read_permission_hash="sha256:" + "4" * 64,
        read_context_hash="sha256:" + "5" * 64,
        connection_target_hash="sha256:" + "6" * 64,
    )
    governance = SchemaGovernance(
        governance_id=str(uuid4()),
        version=1,
        project_id=project_id,
        catalog_hash=schema.content_hash,
        permitted_models=tuple(item.name for item in models),
        business_keys=business_keys,
        recorded_at=now,
        recorded_by="Data manager",
    )
    definition = MappingDefinition(
        mapping_id=str(uuid4()),
        source_selection_hash=mapping_selection.content_hash,
        schema_hash=governance.content_hash,
        datasets=mappings,
        **(
            {"contract_version": mapping_contract_version}
            if mapping_contract_version is not None
            else {}
        ),
    )
    revision = MappingRevision(
        definition.mapping_id,
        1,
        None,
        definition,
        now,
        "Data manager",
    )
    submission = MappingSubmission(
        str(uuid4()),
        definition.mapping_id,
        1,
        definition.content_hash,
        "sha256:" + "7" * 64,
        (),
        now,
        "Data manager",
    )
    ruleset = default_quality_ruleset(
        project_id=project_id,
        mapping_hash=definition.content_hash,
        schema_hash=governance.content_hash,
        datasets=tuple(item.name for item in mapping_selection.datasets),
    )
    recipe_id = str(uuid4())
    data_version_id = str(uuid4())
    recipe = Recipe(
        recipe_id=recipe_id,
        display_name="Representative Recipe",
        business_purpose="Representative Recipe qualification",
        data_classification="INTERNAL",
        retention_days=90,
        current_recipe_revision=None,
        current_data_version_id=data_version_id,
        cutover_candidate_id=None,
        optimistic_revision=1,
        created_at=now,
        updated_at=now,
    )
    data_version = DataVersion(
        data_version_id=data_version_id,
        recipe_id=recipe_id,
        version_number=1,
        workspace_project_id=project_id,
        parent_data_version_id=None,
        purpose=DataVersionPurpose.AUTHORING,
        state=DataVersionState.ACTIVE,
        pinned_recipe_revision=None,
        label="Representative authoring data",
        export_as_of_date=None,
        parameter_values_hash=None,
        created_at=now,
        sealed_at=None,
    )
    evidence = Evidence(
        selection=mapping_selection,
        base_selection=base_selection,
        revision=revision,
        submission=submission,
        schema=schema,
        governance=governance,
        ruleset=ruleset,
        preparation=preparation,
        parameter_definitions=RecipeParameterDefinitions(parameters),
    )
    service = RecipeAuthoringService(
        evidence,
        evidence,
        evidence,
        evidence,
        evidence,
        evidence,
        evidence,
    )

    compiled, issues = service.compile_workspace(project_id)
    if compiled is None:
        raise AssertionError(issues)
    return compiled.recipe


class RepresentativeRecipeShapeTests(unittest.TestCase):
    def test_reviewed_country_reference_compiles_without_primary_schema_capture(
        self,
    ):
        project_id = str(uuid4())
        customers = _dataset(
            "Customers",
            (("customer_code", "string"), ("country_code", "string")),
            "7",
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            project_id,
            datetime.now(timezone.utc),
            "Data manager",
            (customers,),
            "sha256:" + "7" * 64,
        )
        mapping = DatasetMapping(
            dataset_id=customers.dataset_id,
            target_model="res.partner",
            source_identity_column_keys=(_column(customers, "customer_code"),),
            target_identity=(
                IdentityComponentMapping(
                    (_column(customers, "customer_code"),),
                    ("ref",),
                ),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="country_id",
                    kind="many2one",
                    source_column_keys=(_column(customers, "country_code"),),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="res.country",
                        key_mappings=(
                            ReferenceKeyMapping(
                                _column(customers, "country_code"),
                                "code",
                            ),
                        ),
                        value_mappings=(ValueMapping("FRA", "FR"),),
                    ),
                ),
            ),
            approved_write_fields=("country_id",),
        )
        partner_model = SchemaModel(
            "res.partner",
            "Contact",
            (
                _field("ref"),
                _field("country_id", "many2one", relation="res.country"),
            ),
        )
        without_related_capture = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=(mapping,),
            models=(partner_model,),
            business_keys=(_key("res.partner", "ref"),),
            mapping_contract_version=11,
        )
        with_related_capture = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=(mapping,),
            models=(
                partner_model,
                SchemaModel(
                    "res.country",
                    "Country",
                    (
                        _field("code", required=True),
                        _field("name", required=True),
                    ),
                ),
            ),
            business_keys=(_key("res.partner", "ref"),),
            mapping_contract_version=11,
        )

        self.assertEqual(without_related_capture, with_related_capture)
        target_models = {
            item["model"]: item
            for item in without_related_capture["odoo_target_contract"]["models"]
        }
        self.assertEqual(
            target_models["res.country"]["fields"],
            [
                {
                    "field_type": "char",
                    "name": "code",
                    "readonly": False,
                    "required": True,
                    "write_use": False,
                }
            ],
        )
        with self.assertRaisesRegex(
            AssertionError,
            "ODOO_STANDARD_REFERENCE_CHANGED",
        ):
            _publish(
                base_selection=selection,
                mapping_selection=selection,
                mappings=(mapping,),
                models=(
                    partner_model,
                    SchemaModel(
                        "res.country",
                        "Country",
                        (_field("code", required=False),),
                    ),
                ),
                business_keys=(_key("res.partner", "ref"),),
                mapping_contract_version=11,
            )

    def test_product_recipe_compiles_scalar_and_target_reference_meaning(self):
        project_id = str(uuid4())
        products = _dataset(
            "Products",
            (
                ("product_code", "string"),
                ("name", "string"),
                ("uom_code", "string"),
            ),
            "8",
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            project_id,
            datetime.now(timezone.utc),
            "Data manager",
            (products,),
            "sha256:" + "8" * 64,
        )
        mapping = DatasetMapping(
            dataset_id=products.dataset_id,
            target_model="product.template",
            source_identity_column_keys=(_column(products, "product_code"),),
            target_identity=(
                IdentityComponentMapping(
                    (_column(products, "product_code"),),
                    ("default_code",),
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key=_column(products, "name"),
                    required=True,
                    required_on_create=True,
                ),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="uom_id",
                    kind="many2one",
                    source_column_keys=(_column(products, "uom_code"),),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="uom.uom",
                        key_mappings=(
                            ReferenceKeyMapping(
                                _column(products, "uom_code"),
                                "x_external_code",
                            ),
                        ),
                    ),
                    required=True,
                    required_on_create=True,
                ),
            ),
            approved_write_fields=("default_code", "name", "uom_id"),
        )

        recipe = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=(mapping,),
            models=(
                SchemaModel(
                    "product.template",
                    "Product",
                    (
                        _field("default_code"),
                        _field("name", required=True),
                        _field("uom_id", "many2one", relation="uom.uom"),
                    ),
                ),
                SchemaModel("uom.uom", "Unit", (_field("x_external_code"),)),
            ),
            business_keys=(
                _key("product.template", "default_code"),
                _key("uom.uom", "x_external_code"),
            ),
        )

        product = recipe["mapping"]["datasets"][0]
        self.assertEqual(product["target_model"], "product.template")
        self.assertEqual(product["relationships"][0]["target_model"], "uom.uom")
        self.assertEqual(
            product["comparison_policy"]["missing_source_row"],
            "NO_DELETE_INFERENCE",
        )

    def test_product_bom_recipe_compiles_related_preparation_and_dependencies(self):
        project_id = str(uuid4())
        products = _dataset(
            "Products",
            (("product_code", "string"), ("name", "string")),
            "9",
        )
        bom_rows = _dataset(
            "BOM Rows",
            (
                ("bom_code", "string"),
                ("line_number", "integer"),
                ("component_code", "string"),
                ("quantity", "decimal"),
            ),
            "a",
        )
        base = SourceSelection(
            str(uuid4()),
            1,
            project_id,
            datetime.now(timezone.utc),
            "Data manager",
            (products, bom_rows),
            "sha256:" + "9" * 64,
        )
        split = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=bom_rows.dataset_id,
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key=_column(bom_rows, "bom_code"),
            child_key_column_key=_column(bom_rows, "line_number"),
        )
        preparation = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=project_id,
            source_selection_hash=base.content_hash,
            rules=(split,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Data manager",
        )
        derived_binding = DerivedSourceBinding(
            rule_hash="sha256:" + "a" * 64,
            input_dataset_ids=(bom_rows.dataset_id,),
            data_hash=bom_rows.source_evidence_hash,
        )
        boms = SourceDataset(
            dataset_id=f"derived-boms-{uuid4()}",
            name="boms",
            source=derived_binding,
            row_count=2,
            columns=(bom_rows.columns[0],),
        )
        components = SourceDataset(
            dataset_id=f"derived-components-{uuid4()}",
            name="bom_components",
            source=derived_binding,
            row_count=3,
            columns=bom_rows.columns,
        )
        effective = SourceSelection(
            str(uuid4()),
            1,
            project_id,
            datetime.now(timezone.utc),
            "Data manager",
            (products, boms, components),
            "sha256:" + "a" * 64,
        )

        def incoming_product(dataset):
            return RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=products.dataset_id,
                key_mappings=(
                    ReferenceKeyMapping(
                        _column(dataset, "component_code"),
                        "default_code",
                    ),
                ),
            )

        mappings = (
            DatasetMapping(
                dataset_id=products.dataset_id,
                target_model="product.product",
                source_identity_column_keys=(_column(products, "product_code"),),
                target_identity=(
                    IdentityComponentMapping(
                        (_column(products, "product_code"),),
                        ("default_code",),
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        "name",
                        _column(products, "name"),
                        required=True,
                    ),
                ),
                approved_write_fields=("default_code", "name"),
            ),
            DatasetMapping(
                dataset_id=boms.dataset_id,
                target_model="mrp.bom",
                source_identity_column_keys=(_column(boms, "bom_code"),),
                target_identity=(
                    IdentityComponentMapping(
                        (_column(boms, "bom_code"),),
                        ("code",),
                    ),
                ),
                fields=(ScalarFieldMapping("code", _column(boms, "bom_code")),),
                approved_write_fields=("code",),
            ),
            DatasetMapping(
                dataset_id=components.dataset_id,
                target_model="mrp.bom.line",
                source_identity_column_keys=(
                    _column(components, "bom_code"),
                    _column(components, "line_number"),
                ),
                target_identity=(
                    IdentityComponentMapping(
                        (_column(components, "bom_code"),),
                        ("bom_id",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.DATASET,
                            dataset_id=boms.dataset_id,
                            key_mappings=(
                                ReferenceKeyMapping(
                                    _column(components, "bom_code"),
                                    "code",
                                ),
                            ),
                        ),
                    ),
                    IdentityComponentMapping(
                        (_column(components, "line_number"),),
                        ("sequence",),
                        value_type="integer",
                    ),
                ),
                fields=(
                    ScalarFieldMapping(
                        "product_qty",
                        _column(components, "quantity"),
                        value_type="decimal",
                        required=True,
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        "bom_id",
                        "many2one",
                        (_column(components, "bom_code"),),
                        RelationshipResolver(
                            origin=ResolverOrigin.DATASET,
                            dataset_id=boms.dataset_id,
                            key_mappings=(
                                ReferenceKeyMapping(
                                    _column(components, "bom_code"),
                                    "code",
                                ),
                            ),
                        ),
                        required=True,
                    ),
                    RelationshipMapping(
                        "product_id",
                        "many2one",
                        (_column(components, "component_code"),),
                        incoming_product(components),
                        required=True,
                    ),
                ),
                approved_write_fields=(
                    "bom_id",
                    "product_id",
                    "product_qty",
                    "sequence",
                ),
            ),
        )

        recipe = _publish(
            base_selection=base,
            mapping_selection=effective,
            mappings=mappings,
            models=(
                SchemaModel(
                    "product.product",
                    "Product variant",
                    (_field("default_code"), _field("name")),
                ),
                SchemaModel("mrp.bom", "BOM", (_field("code"),)),
                SchemaModel(
                    "mrp.bom.line",
                    "BOM line",
                    (
                        _field("bom_id", "many2one", relation="mrp.bom"),
                        _field("product_id", "many2one", relation="product.product"),
                        _field("product_qty", "float"),
                        _field("sequence", "integer"),
                    ),
                ),
            ),
            business_keys=(
                _key("product.product", "default_code"),
                _key("mrp.bom", "code"),
                _key("mrp.bom.line", "bom_id", "sequence"),
            ),
            preparation=preparation,
        )

        self.assertEqual(
            recipe["source_preparation"]["rules"][0]["kind"],
            "parent_child",
        )
        by_model = {
            item["target_model"]: item for item in recipe["mapping"]["datasets"]
        }
        line = by_model["mrp.bom.line"]
        self.assertEqual(
            {item["target_dataset_id"] for item in line["relationships"]},
            {"dataset:boms", "dataset:products"},
        )

    def test_stock_recipe_declares_fresh_parameters_and_quantity_control(self):
        project_id = str(uuid4())
        stock = _dataset(
            "Stock Levels",
            (
                ("product_code", "string"),
                ("warehouse_code", "string"),
                ("quantity", "decimal"),
            ),
            "c",
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            project_id,
            datetime.now(timezone.utc),
            "Data manager",
            (stock,),
            "sha256:" + "c" * 64,
        )
        mapping = DatasetMapping(
            dataset_id=stock.dataset_id,
            target_model="stock.quant",
            source_identity_column_keys=(
                _column(stock, "product_code"),
                _column(stock, "warehouse_code"),
            ),
            target_identity=(
                IdentityComponentMapping(
                    (_column(stock, "product_code"),),
                    ("product_id",),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="product.product",
                        key_mappings=(
                            ReferenceKeyMapping(
                                _column(stock, "product_code"),
                                "default_code",
                            ),
                        ),
                    ),
                ),
                IdentityComponentMapping(
                    (_column(stock, "warehouse_code"),),
                    ("location_id",),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="stock.location",
                        key_mappings=(
                            ReferenceKeyMapping(
                                _column(stock, "warehouse_code"),
                                "x_warehouse_code",
                            ),
                        ),
                    ),
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    "inventory_quantity",
                    _column(stock, "quantity"),
                    value_type="decimal",
                    required=True,
                ),
            ),
            approved_write_fields=("inventory_quantity",),
            control_definitions=(
                BusinessControlDefinition(
                    control_id="stock-quantity",
                    name="Expected stock quantity",
                    target_field="inventory_quantity",
                    unit="units",
                ),
            ),
        )

        recipe = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=(mapping,),
            models=(
                SchemaModel(
                    "stock.quant",
                    "Stock",
                    (
                        _field("product_id", "many2one", relation="product.product"),
                        _field("location_id", "many2one", relation="stock.location"),
                        _field("inventory_quantity", "float"),
                    ),
                ),
                SchemaModel("product.product", "Product", (_field("default_code"),)),
                SchemaModel(
                    "stock.location",
                    "Location",
                    (_field("x_warehouse_code"),),
                ),
            ),
            business_keys=(
                _key("stock.quant", "product_id", "location_id"),
                _key("product.product", "default_code"),
                _key("stock.location", "x_warehouse_code"),
            ),
            parameters=(RecipeParameterDefinition("warehouse", "Warehouse", "string"),),
        )

        parameter_ids = {
            item["logical_parameter_id"]
            for item in recipe["parameter_definitions"]["parameters"]
        }
        self.assertEqual(
            parameter_ids,
            {"parameter:export_as_of_date", "parameter:warehouse"},
        )
        control = recipe["control_definitions"]["controls"][0]
        self.assertEqual(control["calculation"], "SUM")
        self.assertFalse(control["invariant_expectation"])
        self.assertNotIn("invariant_expected_total", control)
        first_values = RecipeApplicationService._control_values(
            (control,),
            {control["logical_control_id"]: "1250.50"},
        )
        second_values = RecipeApplicationService._control_values(
            (control,),
            {control["logical_control_id"]: "900"},
        )
        first = RecipeControlValues(
            data_version_id=str(uuid4()),
            values=first_values,
            actor=LOCAL_ACTOR.identity,
            confirmed_at=datetime.now(timezone.utc),
        )
        second = RecipeControlValues(
            data_version_id=str(uuid4()),
            values=second_values,
            actor=LOCAL_ACTOR.identity,
            confirmed_at=datetime.now(timezone.utc),
        )
        self.assertEqual(first_values[control["logical_control_id"]], "1250.50")
        self.assertEqual(second_values[control["logical_control_id"]], "900")
        self.assertNotEqual(first.content_hash, second.content_hash)


if __name__ == "__main__":
    unittest.main()
